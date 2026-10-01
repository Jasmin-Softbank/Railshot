// Shared with the CI workflow admission rules; target IDs name operator configuration.
export const APP_NAME = /^[a-z][a-z0-9-]{1,28}[a-z0-9]$/;
export const TENANT_NAME = /^[a-z0-9]{1,20}$/;
export const TARGET_ID = /^[a-z][a-z0-9-]{0,62}$/;
export const SOURCE_COMMIT = /^[a-f0-9]{40}$/;
export const APP_NAME_MESSAGE = '앱 이름은 영문 소문자로 시작하고 영문 소문자나 숫자로 끝나는 3~30자여야 합니다. 중간에 하이픈을 사용할 수 있습니다.';
