"""Download pinned official Windows portable runtimes into this project only."""
import hashlib
import json
import time
import urllib.request
import zipfile
import httpx
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
ASSETS = [("ollama/ollama", "v0.40.5", "ollama-windows-amd64.zip", "ollama-v0.40.5"),
          ("qdrant/qdrant", "v1.14.1", "qdrant-x86_64-pc-windows-msvc.zip", "qdrant")]


def download(repo, version, filename, destination):
    info_url = f"https://api.github.com/repos/{repo}/releases/tags/{version}"
    request = urllib.request.Request(info_url, headers={"User-Agent": "LocalDocRAG-native-setup"})
    info = json.load(urllib.request.urlopen(request, timeout=30))
    asset = next(a for a in info["assets"] if a["name"] == filename)
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / 'release.json'
    if marker.is_file() and (destination / ('qdrant.exe' if repo.startswith('qdrant/') else 'ollama.exe')).is_file():
        installed = json.loads(marker.read_text(encoding='utf-8'))
        if installed.get('repo') == repo and installed.get('tag') == version and (not asset.get('digest') or asset['digest'] == 'sha256:' + installed.get('sha256','')):
            print(f'Already installed: {destination.name} {version}', flush=True)
            return
    archive = RUNTIME / filename
    if not archive.is_file() or archive.stat().st_size != asset["size"]:
        temporary = archive.with_suffix(".part")
        started = previous = time.monotonic()
        count = temporary.stat().st_size if temporary.exists() else 0
        if count > asset['size']:
            raise RuntimeError('Partial download exceeds official asset size')
        # Small, resumable ranges avoid losing a 1GB download on a stalled CDN
        # connection. No document data is sent: only public runtime assets.
        with httpx.Client(timeout=30,follow_redirects=True,trust_env=False) as client, temporary.open('ab') as target:
            while count < asset['size']:
                end=min(asset['size']-1,count+16*1024*1024-1)
                for attempt in range(4):
                    try:
                        response=client.get(asset['browser_download_url']+'?localdoc=1',headers={'Range':f'bytes={count}-{end}'})
                        response.raise_for_status()
                        if response.status_code!=206 or response.headers.get('content-range')!=f'bytes {count}-{end}/{asset["size"]}' or len(response.content)!=end-count+1:
                            raise RuntimeError('Official download returned an unexpected range')
                        break
                    except (httpx.HTTPError,RuntimeError):
                        if attempt==3: raise
                        time.sleep(attempt+1)
                target.write(response.content);target.flush();count+=len(response.content)
                if time.monotonic()-previous>10:
                    print(f"{filename}: {count/1048576:.0f}/{asset['size']/1048576:.0f} MiB",flush=True)
                    previous=time.monotonic()
        if count != asset["size"]:
            raise RuntimeError("Incomplete portable runtime download")
        temporary.replace(archive)
    digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    expected = asset.get("digest")
    if expected and expected != "sha256:" + digest:
        raise RuntimeError("Official release checksum mismatch")
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            resolved = (destination / member.filename).resolve()
            if not resolved.is_relative_to(destination.resolve()):
                raise RuntimeError("Unsafe release archive path")
        zipped.extractall(destination)
    (destination / "release.json").write_text(json.dumps({"repo": repo, "tag": version, "asset": filename, "sha256": digest}, indent=2), encoding="utf-8")
    print(f"Ready: {destination.name} {version}", flush=True)


if __name__ == "__main__":
    RUNTIME.mkdir(exist_ok=True)
    # Qdrant is small; make it available while the larger Ollama bundle downloads.
    for repo, version, filename, folder in reversed(ASSETS):
        download(repo, version, filename, RUNTIME / folder)
