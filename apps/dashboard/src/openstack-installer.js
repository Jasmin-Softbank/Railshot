import { request } from './api.js';

export function initializeOpenStackInstaller() {
  const openstackInstallResult = document.querySelector('#openstack-installer-result');
  const openstackInstallStatus = document.querySelector('#openstack-install-status');
  const openstackEnrollmentKey = document.querySelector('#openstack-enrollment-key');

  async function prepareOpenStackInstaller() {
    const button = document.querySelector('#prepare-openstack-install');
    const enrollmentKey = openstackEnrollmentKey.value;
    openstackInstallResult.hidden = true;
    document.querySelector('#openstack-linkage-token').value = '';
    openstackInstallStatus.textContent = '';
    if (enrollmentKey.length < 16 || enrollmentKey.length > 256 || enrollmentKey.trim() !== enrollmentKey
        || /[\x00-\x1f\x7f]/.test(enrollmentKey)) {
      openstackInstallStatus.textContent = '16~256자의 연계 키를 확인하세요.';
      return;
    }
    button.disabled = true;
    try {
      const { data: installer } = await request('/api/v1/installers/openstack');
      if (typeof installer.install_sh !== 'string' || !installer.install_sh.startsWith('#!/usr/bin/env bash')
          || installer.script_url !== '/api/v1/installers/openstack/scripts'
          || installer.bundle_url !== '/api/v1/installers/openstack/bundles'
          || installer.token_client_url !== '/api/v1/installers/openstack/client'
          || !/^[a-f0-9]{64}$/.test(installer.bundle_sha256)) throw new Error('설치 파일 응답을 확인하지 못했습니다.');
      const { data: registration } = await request('/api/v1/registrations', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: 'openstack', enrollment_key: enrollmentKey }) });
      if (!/^[a-f0-9-]{36}$/.test(registration.id || '')
          || !/^rsl_[A-Za-z0-9_-]{43}$/.test(registration.linkage_token || '')
          || !Number.isFinite(Date.parse(registration.token_expires_at))) throw new Error('등록 결과를 확인하지 못했습니다.');
      document.querySelector('#openstack-linkage-token').value = registration.linkage_token;
      document.querySelector('#openstack-token-expires').textContent = `토큰 만료: ${new Date(registration.token_expires_at).toLocaleString()}. 원문은 이 화면에서만 볼 수 있습니다.`;
      document.querySelector('#openstack-install-script').value = installer.install_sh;
      document.querySelector('#openstack-script-download').href = installer.script_url;
      document.querySelector('#openstack-bundle-download').href = installer.bundle_url;
      document.querySelector('#openstack-token-client-download').href = installer.token_client_url;
      openstackEnrollmentKey.value = '';
      openstackInstallResult.hidden = false;
      openstackInstallStatus.textContent = '등록 요청을 저장하고 일회성 연계 토큰을 발급했습니다.';
    } catch (cause) {
      openstackInstallStatus.textContent = cause.name === 'AbortError' ? '요청 시간이 초과되었습니다. 등록 상태를 확인하세요.' : cause.message;
    } finally { button.disabled = false; }
  }
  document.querySelector('#prepare-openstack-install').addEventListener('click', prepareOpenStackInstaller);
  for (const [button, source] of [['#copy-openstack-token', '#openstack-linkage-token'],
    ['#copy-openstack-script', '#openstack-install-script']]) {
    document.querySelector(button).addEventListener('click', async () => {
      const field = document.querySelector(source);
      try { await navigator.clipboard.writeText(field.value); openstackInstallStatus.textContent = '복사했습니다.'; }
      catch { field.focus(); field.select(); openstackInstallStatus.textContent = '내용을 선택했습니다. 직접 복사하세요.'; }
    });
  }
}
