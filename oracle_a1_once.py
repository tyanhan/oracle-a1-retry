#!/usr/bin/env python3
"""Attempt one Oracle Always Free A1 launch (GitHub Actions edition).

Derived from the user-provided oracle_a1_retry script. This version tries ONCE
per invocation; scheduling is the responsibility of GitHub Actions.

It never creates network resources and it stops on non-capacity errors.
GitHub Actions results: created / exists / capacity / validated.
"""

import argparse
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

try:
    import oci
except ImportError:
    print("Missing Oracle SDK. Run: python -m pip install oci", file=sys.stderr)
    sys.exit(2)

REGION = "ap-singapore-1"
SHAPE = "VM.Standard.A1.Flex"
VCN_NAME = "ai-dev-vcn"
SUBNET_NAME = "ai-dev-subnet"
INSTANCE_NAME = "ai-dev-ubuntu"
IMAGE_PREFIX = "Canonical-Ubuntu-24.04-aarch64-"


def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def github_result(result):
    """Send a non-secret outcome to GitHub Actions if running there."""
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            file.write(f"result={result}\n")


def all_results(fn, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(fn, *args, **kwargs).data


def exactly_one(items, label):
    if len(items) != 1:
        raise RuntimeError(f"Expected exactly one {label}; found {len(items)}. Check the compartment/name.")
    return items[0]


def discover_network(network, compartment_id):
    vcn = exactly_one(
        [v for v in all_results(network.list_vcns, compartment_id)
         if v.display_name == VCN_NAME and v.lifecycle_state == "AVAILABLE"],
        f"available VCN named {VCN_NAME}",
    )
    subnet = exactly_one(
        [s for s in all_results(network.list_subnets, compartment_id, vcn_id=vcn.id)
         if s.display_name == SUBNET_NAME and s.lifecycle_state == "AVAILABLE"],
        f"available subnet named {SUBNET_NAME} in {VCN_NAME}",
    )
    if subnet.prohibit_public_ip_on_vnic:
        raise RuntimeError("Subnet prohibits public IPs. Change to a public subnet before continuing.")

    # Network readiness warning only: routing and security rules can be corrected later.
    try:
        route_table = network.get_route_table(subnet.route_table_id).data
        internet_route = any(
            r.destination == "0.0.0.0/0" and r.network_entity_id
            for r in (route_table.route_rules or [])
        )
        if not internet_route:
            log("WARNING: No 0.0.0.0/0 route found for this subnet. Configure an Internet Gateway before SSH.")
    except oci.exceptions.ServiceError as exc:
        log(f"WARNING: Could not verify subnet routing ({exc.status}/{exc.code}).")
    log(f"VCN found: {vcn.display_name}")
    log(f"Subnet found: {subnet.display_name}")
    return subnet


def discover_image(compute, compartment_id):
    images = all_results(compute.list_images, compartment_id, shape=SHAPE)
    # OCI uses different image OCIDs for aarch64 and x86_64. Exclude Minimal.
    matching = [im for im in images if (im.display_name or "").startswith(IMAGE_PREFIX)]
    if not matching:
        raise RuntimeError(
            "No standard Ubuntu 24.04 ARM64 image found for A1. "
            "Check your region or choose a different platform-image build."
        )
    matching.sort(key=lambda im: im.time_created, reverse=True)
    image = matching[0]
    log(f"Image selected: {image.display_name}")
    return image


def find_existing_instance(compute, compartment_id):
    instances = all_results(compute.list_instances, compartment_id)
    active = [i for i in instances if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
    named = [i for i in active if i.display_name == INSTANCE_NAME]
    if named:
        info = named[0]
        return f"Instance already exists: {info.display_name} ({info.lifecycle_state})"
    a1 = [i for i in active if i.shape == SHAPE]
    if a1:
        return (
            "An existing A1 instance is already in this compartment: "
            + ", ".join(f"{i.display_name} ({i.lifecycle_state})" for i in a1)
            + ". Stopping to protect free-tier allocation."
        )
    return None


def is_out_of_capacity(exc):
    msg = str(getattr(exc, "message", exc)).lower()
    return any(
        phrase in msg
        for phrase in (
            "out of host capacity",
            "out of capacity for shape",
            "out of capacity",
            "insufficient host capacity",
        )
    )


def main():
    p = argparse.ArgumentParser(description="Attempt one Always Free Oracle A1 launch in Singapore.")
    p.add_argument("--ssh-public-key", required=True, help="Path to the OpenSSH .pub key used to access the VM")
    p.add_argument("--ocpus", type=int, choices=[1, 2], default=2)
    p.add_argument("--memory-gb", type=int, choices=[6, 12], default=12)
    p.add_argument("--boot-gb", type=int, default=100)
    p.add_argument("--compartment-id", help="Compartment OCID (defaults to tenancy/root compartment)")
    p.add_argument("--config-file", default=str(Path.home() / ".oci" / "config"))
    p.add_argument("--profile", default="DEFAULT")
    p.add_argument("--dry-run", action="store_true", help="Validate configuration, do not create anything")
    args = p.parse_args()

    if args.boot_gb < 50 or args.boot_gb > 100:
        p.error("This safety-limited example allows only a 50-100 GB boot volume.")
    if (args.ocpus == 1 and args.memory_gb != 6) or (args.ocpus == 2 and args.memory_gb != 12):
        p.error("Use either 1 OCPU + 6 GB or 2 OCPUs + 12 GB.")

    pubkey_path = Path(args.ssh_public_key).expanduser()
    if not pubkey_path.is_file():
        p.error(f"SSH public key not found: {pubkey_path}")
    ssh_key = pubkey_path.read_text(encoding="utf-8-sig").strip()
    if "\n" in ssh_key or not ssh_key.startswith(("ssh-rsa ", "ssh-ed25519 ", "ecdsa-sha2-")):
        p.error("Provide a single-line OpenSSH PUBLIC key (.pub); do not use the private key or an OCI API signing key.")

    config = oci.config.from_file(file_location=args.config_file, profile_name=args.profile)
    oci.config.validate_config(config)
    if config.get("region") != REGION:
        raise RuntimeError(f"OCI config region must be {REGION}; found {config.get('region')!r}.")
    compartment_id = args.compartment_id or config["tenancy"]

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)
    # Root tenancy's AD name uses an Oracle-specific tenancy prefix, e.g. KCdw:AP-SINGAPORE-1-AD-1.
    ads = identity.list_availability_domains(compartment_id).data
    if not ads:
        raise RuntimeError("OCI returned no availability domains.")
    ad_name = ads[0].name
    subnet = discover_network(network, compartment_id)
    image = discover_image(compute, compartment_id)

    log(f"Region: {REGION}; AD: {ad_name}")
    log(f"Shape: {SHAPE}, {args.ocpus} OCPU / {args.memory_gb} GB RAM")
    log(f"Boot volume: {args.boot_gb} GB @ 10 VPUs/GB; ephemeral public IPv4 enabled")
    log("GitHub Actions mode: one request per invocation; no internal sleep/retry loop")
    log("Cost reminder: check your home region, other compartments, existing volumes and Always Free usage yourself.")

    existing = find_existing_instance(compute, compartment_id)
    if existing:
        log(existing)
        github_result("exists")
        return 0
    if args.dry_run:
        log("Dry run OK: no instance created.")
        github_result("validated")
        return 0

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad_name,
        compartment_id=compartment_id,
        display_name=INSTANCE_NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=args.ocpus,
            memory_in_gbs=args.memory_gb,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=image.id,
            boot_volume_size_in_gbs=args.boot_gb,
            boot_volume_vpus_per_gb=10,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet.id,
            display_name="ai-dev-vnic",
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_key},
        is_pv_encryption_in_transit_enabled=True,
    )

    # Only one launch attempt. GitHub is the external 5-minute scheduler.
    log("Launch attempt...")
    try:
        response = compute.launch_instance(
            details,
            opc_retry_token=str(uuid.uuid4()),
            retry_strategy=oci.retry.NoneRetryStrategy(),
        )
        instance = response.data
        log(f"SUCCESS: Oracle accepted launch! State: {instance.lifecycle_state}")
        log(f"Name: {instance.display_name}")
        log("Check OCI Console -> Compute -> Instances for provisioning and public IP.")
        github_result("created")
        return 0
    except oci.exceptions.ServiceError as exc:
        if is_out_of_capacity(exc):
            log("Capacity unavailable. A later scheduled run can try again.")
            github_result("capacity")
            return 0
        log(f"NON-CAPACITY ERROR: HTTP {exc.status} / {exc.code}: {exc.message}")
        if exc.status == 429:
            log("Rate-limited: stopping; do not increase request frequency.")
        else:
            log("Stopping: fix IAM policy, quota, image or configuration before retrying.")
        github_result("error")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Stopped by user. No further attempts.")
        sys.exit(130)
    except Exception as error:
        log(f"ERROR: {type(error).__name__}: {error}")
        sys.exit(1)
