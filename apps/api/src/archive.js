import { promisify } from 'node:util';
import yauzl from 'yauzl';

const openZip = promisify(yauzl.fromBuffer);
const MAX_ARCHIVE_BYTES = 100 * 1024 * 1024;
const MAX_FILES = 2000;

// The deployment service consumes this file tree regardless of upload format.
export function validateFiles(files) {
  if (!Array.isArray(files)) throw new Error('파일 목록이 잘못되었습니다.');
  if (!files.length) throw new Error('배포할 파일이 없습니다.');
  if (files.length > MAX_FILES) throw new Error('파일은 최대 2,000개까지 업로드할 수 있습니다.');
  let totalSize = 0;
  const paths = new Set();
  const accepted = [];
  for (const file of files) {
    if (!file || !Buffer.isBuffer(file.content)) throw new Error('파일 내용이 잘못되었습니다.');
    const path = file.path;
    const parts = typeof path === 'string' ? path.split('/') : [];
    if (!path || path.startsWith('/') || path.includes('\\') || path.includes('\0') || parts.some((part) => !part || part === '.' || part === '..')) {
      throw new Error(`안전하지 않은 파일 경로: ${path}`);
    }
    if (parts.some((part) => ['.git', 'node_modules', '__MACOSX', '.DS_Store'].includes(part))) continue;
    if (parts.some((part) => /^\.env(?:\.|$)/i.test(part) || /\.(?:pem|key|p12|pfx)$/i.test(part) || /^id_(rsa|ed25519|ecdsa)$/i.test(part))) {
      throw new Error(`비밀키로 보이는 파일을 제거하세요: ${path}`);
    }
    if (paths.has(path)) throw new Error(`같은 경로의 파일이 중복되어 있습니다: ${path}`);
    paths.add(path);
    totalSize += file.content.length;
    if (totalSize > MAX_ARCHIVE_BYTES) throw new Error('파일 총 크기는 100 MB 이하여야 합니다.');
    accepted.push(file);
  }
  if (!accepted.length) throw new Error('배포할 파일이 없습니다.');
  return accepted;
}

function streamToBuffer(stream, expectedSize) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    stream.on('data', (chunk) => {
      size += chunk.length;
      if (size > expectedSize || size > MAX_ARCHIVE_BYTES) {
        stream.destroy(new Error('ZIP 항목의 크기가 허용 범위를 넘었습니다.'));
        return;
      }
      chunks.push(chunk);
    });
    stream.once('error', reject);
    stream.once('end', () => resolve(Buffer.concat(chunks)));
  });
}

export async function inspectArchive(bytes, { stripRoot = false } = {}) {
  if (!bytes.length || bytes.length > MAX_ARCHIVE_BYTES) {
    throw new Error('ZIP 파일은 100 MB 이하이어야 합니다.');
  }
  const zip = await openZip(bytes, { lazyEntries: true, decodeStrings: true, validateEntrySizes: true });
  const files = [];
  let totalSize = 0;
  await new Promise((resolve, reject) => {
    zip.once('error', reject);
    zip.once('end', resolve);
    zip.on('entry', async (entry) => {
      try {
        const name = entry.fileName.replaceAll('\\', '/');
        const parts = name.split('/');
        const mode = (entry.externalFileAttributes >>> 16) & 0o170000;
        if (name.startsWith('/') || parts.includes('..') || parts.includes('.') || name.includes('\0') || mode === 0o120000) {
          throw new Error(`안전하지 않은 ZIP 경로: ${name}`);
        }
        if (name.endsWith('/')) { zip.readEntry(); return; }
        if (!name || parts.some((part) => part === '.git' || part === 'node_modules' || part === '__MACOSX')) { zip.readEntry(); return; }
        if (parts.some((part) => /^\.env(?:\.|$)/i.test(part) || /\.(?:pem|key|p12|pfx)$/i.test(part))) {
          throw new Error(`비밀키로 보이는 파일을 ZIP에서 제거하세요: ${name}`);
        }
        totalSize += entry.uncompressedSize;
        if (files.length >= MAX_FILES || totalSize > MAX_ARCHIVE_BYTES) {
          throw new Error('ZIP은 파일 2,000개와 압축 해제 크기 100 MB 이하여야 합니다.');
        }
        zip.openReadStream(entry, async (error, stream) => {
          if (error) { reject(error); zip.close(); return; }
          try {
            files.push({ path: name, content: await streamToBuffer(stream, entry.uncompressedSize) });
            zip.readEntry();
          } catch (cause) { reject(cause); zip.close(); }
        });
      } catch (cause) { reject(cause); zip.close(); }
    });
    zip.readEntry();
  });
  if (!files.length) throw new Error('ZIP에 배포할 파일이 없습니다.');
  const top = files[0].path.split('/')[0];
  const commonRoot = !top.startsWith('.')
    && files.every((file) => file.path.startsWith(`${top}/`))
    && files.every((file) => file.path.length > top.length + 1);
  if (stripRoot && !commonRoot) throw new Error('GitHub 소스의 최상위 폴더를 확인할 수 없습니다.');
  const hasWrapper = commonRoot
    && files.some((file) => ['package.json', 'Dockerfile', '.jasmin/jasmin.yaml'].includes(file.path.slice(top.length + 1)));
  const normalized = files.map((file) => ({ ...file, path: stripRoot || hasWrapper ? file.path.slice(top.length + 1) : file.path }));
  return validateFiles(normalized);
}

export const archiveLimits = { maxBytes: MAX_ARCHIVE_BYTES, maxFiles: MAX_FILES };
