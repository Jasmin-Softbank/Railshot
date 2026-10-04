# Personal OpenStack slot preparation — 2026-10-04

This is one integration-test slot, not automatic onboarding for arbitrary customer clouds.
A dedicated load balancer and listener are now ACTIVE/ONLINE and pass strict TLS plus
unmatched-host HTTP 503 checks. Two grants still require explicit approval: access by the
new project to the existing private network, and the dedicated SSH account with its fixed
sudo command. Gateway and central runtime activation remain pending. Central configuration
readiness alone does not prove successful customer VM creation.

## Completed and verified

- Created project `railshot` in domain `default`:
  `c00d277173c540ab8ad2f1d886f4164b`.
- The installer can reuse that project without claiming project deletion ownership.
- Controller: `railshot-octavia-control-01`, `10.0.0.34`.
  The AWS relay `172.31.0.172:10022` reaches the existing runtime at `10.0.0.17`;
  that SSH connection forwards to a second SSH connection at `10.0.0.34:22`.
- Root-only project creation receipt:
  `/var/lib/railshot-personal-slot-ops/slot-20261004/prerequisites.json` on the controller.
  Its only operation is `project: succeeded`. No network or image sharing mutation ran.
  A separate new private-image copy succeeded later, as described below.
- Pre-load-balancer placement capacity snapshot: VCPU 7/32 (8 physical CPUs, allocation ratio 4),
  memory 11,264/31,578 MiB, disk 110/192 GiB. A placement candidate exists for
  an additional 3 VCPU, 5,120 MiB and 50 GiB (one Amphora plus a customer VM).
  This snapshot preceded the new Amphora allocation and is not a current free-capacity claim.
- Existing slot wrapper tests: 8 passed using
  `/private/tmp/jasmin-bootstrap-verify/bin/python -m pytest deployment/scripts/tests/test_personal_slot_worker.py -q`.
  These are local unit tests, not remote installation verification.
- `personal_slot_config.py` is a pure renderer for six central JSON documents and writes
  no files or cloud resources. Eleven tests cover evidence consistency, source preservation,
  CIDR representation and rejection of unready/cross-bound resources or the legacy tunnel.

## Dedicated ingress verified

- Load balancer: `bf391f1c-ef5f-4558-91bd-3b6545c2ce7b`,
  `railshot-personal-slot-20261004`, `ACTIVE`/`ONLINE`, VIP `10.0.0.45`.
- Listener: `2f61898c-049d-42b9-bb22-333c30e9fb65`, enabled,
  `ACTIVE`/`ONLINE`, HTTPS 443 with TLS termination, no default pool,
  allowed source `10.0.0.0/26`.
- Amphora: `4d5e6fd1-7d8d-44b6-9cf6-d349151fdf1e`, `ALLOCATED`/`STANDALONE`;
  server `b9dfaa1f-5ddb-40ed-9493-f50be6e0c9a5` is `ACTIVE`.
- Service port: `7c59cfe9-c99a-43a0-88c9-41eb5c5a3b69`, address `10.0.0.33`;
  management address `10.77.0.239`.
- From the management controller, `https://personal-slot.railshot.io/` resolved to
  the new VIP returned HTTP 503 with certificate verification successful. This checks
  TLS and rejection of an unmapped hostname; it does not prove application serving.
- Existing load balancer ID, name, project, VIP and listener relation were unchanged.
- Creation used the `amphora` provider, project `8de0a5b10e974e71b575cd16ff55d6df`,
  and subnet `a1b31a94-0fa1-4ae0-81b4-c64d2c854216`. The Amphora uses flavor `d2`:
  1 VCPU, 1 GiB RAM and 10 GiB disk. The listener was created disabled, read back, then enabled.
- Existing certificate reference `e0aec211-ba33-4850-bd66-45acdf87dd97` was reused read-only.
  It covers `*.railshot.io` and verifies against `/opt/railshot/octavia/origin-ca/ca.pem`.
  It expires **2026-12-31 22:56:54 UTC** and needs renewal before then.
- Root-only receipts on the controller: `loadbalancer.json` and `slot-evidence.json`
  under `/var/lib/railshot-personal-slot-ops/slot-20261004`.

## Approval pending 1: project-specific network grant

Automatic approval review rejected granting persistent access to the existing private
network because the human user had not explicitly approved this security boundary change.
Do not retry or work around that rejection without the necessary authorization.

Proposed rule:

| Field | Value |
|---|---|
| Object type | `network` |
| Network | `3d30be98-e8b6-45ff-84cb-73a711132f74` (`private`) |
| Owner | `dce8e4d6abc4487fa1b197c634b4d29b` (`demo`) |
| Action | `access_as_shared` |
| Target project | `c00d277173c540ab8ad2f1d886f4164b` (`railshot`) |
| Subnet | `a1b31a94-0fa1-4ae0-81b4-c64d2c854216`, `10.0.0.0/26` |
| Existing global shared value | `false`; do not change to globally shared |
| Lifetime | OpenStack RBAC has no automatic expiry. Keep only for this test slot and explicitly remove after customer ports are removed. |

Neutron assigns a rule UUID; this resource has no operator name or automatic TTL.
Record creation intent, exact target and returned UUID before continuing. With this grant,
new project members may attach ports to this existing private network. Existing ports and
security groups must remain unchanged. Delete only the recorded rule during rollback,
after verifying no new-project ports depend on it. A rule deletion while ports depend on
it may be rejected; do not remove unrelated ports to force cleanup.

## Private image copy completed and verified

Source image `5726fd60-813f-4185-92bb-d130a163eafe` (`oslab-ubuntu-24.04`) is owned by
`8de0a5b10e974e71b575cd16ff55d6df` (`admin`) and has visibility `private`.
It is active, qcow2/bare, 625,612,288 bytes, checksum
`0a01cf41ac7b059e4ada1105227c7e8d`. Controller scratch space has 37,031,292,928 free bytes.
Glance advertises default image-size and staging limits of 2,000 and an image-count
limit of 100; no project-specific overrides were returned. The completed upload is verified below.

A new private copy was created and verified without changing source visibility:

- ID: `afd8aa58-fab8-4eef-a365-3a7fec7fe955`.
- Name: `railshot-personal-ubuntu-2404-slot-20261004-copy2`.
- Owner: `c00d277173c540ab8ad2f1d886f4164b` (`railshot`).
- State: `active`, visibility `private`, qcow2/bare, 625,612,288 bytes.
- MD5 checksum and SHA-512 content hash match the downloaded source.
- Source ID, owner, private visibility, active state, checksum and size remain unchanged.
- The root-only scratch blob and its directory were removed after successful verification.
- Receipt: `/var/lib/railshot-personal-slot-ops/slot-20261004/image-copy-attempt2.json`.
  It records the preassigned new UUID and each POST/PUT HTTP status and request ID.

The first CLI metadata-creation attempt returned nonzero. Exact-name, target-owner,
and `visibility=all` Glance queries each returned a complete single page with no image.
That historical `image-copy.json` retains `download=succeeded`, `create=pending`;
it is not a retry instruction. The successful second attempt used a distinct name and
preassigned UUID, direct metadata POST, then a PUT to that exact image ID.
Rollback removes only the verified new image after checking customer-server dependencies.

Do not use `openstack image set --accept --project ...` to accept membership:
the installed client also sets `owner_id` in that code path. Source sharing, if ever
selected instead of copying, would require separate approval and exact member API calls.

## Approval pending 2: dedicated SSH account and fixed sudo command

The worker installation was not executed. Automatic review rejected adding persistent
SSH/sudo access with a script that also used the reserved variable name `HOME` for its
new account directory. The local candidate now uses `slot_home`; its syntax and fixed-path
checks pass. No remote retry was performed. Approval must cover the persistent access grant.

The candidate is `/private/tmp/railshot-slot-worker-install.py`. Its proposed scope is:

- Controller `10.0.0.34`: new system account `railshot-personal-slot`, home
  `/var/lib/railshot-personal-slot`. Home, `.ssh` and `authorized_keys` belong to this
  account with modes 0700/0700/0600.
- One dedicated public key, restricted to source `10.0.0.17` and forced command
  `sudo -n /usr/bin/python3 -I /opt/railshot/personal-slot/slot-20261004/entry.py`.
  SSH forwarding and terminal allocation are disabled by `restrict`.
- A new root-owned 0440 `/etc/sudoers.d/railshot-personal-slot` allows only that exact
  Python command and argument combination after `visudo` validation.
- Root-owned 0700 `/opt/railshot/personal-slot/slot-20261004` contains separate worker,
  state and certificate directories. Its wrapper runs the fixed protocol inside a private
  mount namespace; it does not replace the existing worker or change global SSH settings.
- If `/opt/railshot/personal-slot` already exists, the installer only checks that it is
  a root-owned directory without group/world write permission. It does not change that
  directory's ownership or permissions. If absent, it creates this new parent with 0700.
  The slot directory itself must be absent and is created separately; recursive creation
  and parent chmod/chown are not used.

A fresh controller key already exists in the AWS PVC at
`config/personal/slot-20261004/controller_identity` (root 0600). No new controller account,
authorized-key entry, sudoers file or worker files have been installed. Remove or disable
only the new account's key and sudoers entry for rollback; remove its state and directories
only after confirming there are no customer bindings or operations to retain.

## Gateway and central runtime activation pending

These activation steps are separate from the completed image, load balancer and listener:

1. After explicit approval, apply and read back the two grants above. Verify the dedicated
   worker's command restriction, file digests, mount isolation and legacy file/state preservation.
2. Review and install the six central runtime documents already rendered from actual provider
   readback under `/private/tmp/railshot-personal-slot-review`. They are review files only;
   no production configuration was installed from them. The selector is
   `management_network=private`, `placement=nova`; the dedicated Cloudflare tunnel is
   `3b5be82d-205b-45e0-8910-a23952f4948d`. Keep its credentials private and separate.
3. Complete gateway activation and central API wiring, then verify the gateway health check,
   immutable installer/artifact downloads and runtime configuration checks. Only after those
   checks pass should the public readiness response and installer-command display be verified.
4. The customer's installer creates or selects its application VM. Only then can the worker's
   `verify-runtime` succeed: it requires a real VM in a project named `railshot`, an exclusive
   security group and a port on the slot subnet. The completed TLS and preflight checks do not
   prove that later customer-VM registration has succeeded.

The current Keystone endpoint uses HTTP. A separately authorized single-client test can
pass the existing `--test-allow-http` flag to that installation while retaining HTTPS for
the public API/downloads. Avoid a server-wide HTTP exception merely for one client test.
The existing fully shared network has no router interface and no verified management
reachability; it is not a ready substitute for the private network.

## Rollback boundaries

- New project deletion is allowed only if this receipt still identifies it, it has no
  customer VMs/ports/images/credentials/role assignments, and no later installer owns work
  there. Never delete by name alone. The current installer deliberately retains projects.
- Remove only the journaled new LB/listener/Amphora resources after checking slot dependencies. Never delete or
  modify existing LB `0265bd05-8114-4acb-9dc0-46f5d898a9ad`, existing runtime, or its routes.
- The old LB's current operating ERROR comes from one old application member on port
  32149; its listener and two other members were ONLINE. This finding is outside this slot's scope.
