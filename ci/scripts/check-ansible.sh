#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root/infrastructure/ansible"
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ansible-inventory -i inventories/example/hosts.yml --graph
for playbook in playbooks/*.yml; do
  ansible-playbook -i inventories/example/hosts.yml "$playbook" --syntax-check
done
ansible-lint --offline .
python3 "$repo_root/ci/scripts/check_contract.py"
python3 -m pytest "$repo_root/ci/tests" -q
