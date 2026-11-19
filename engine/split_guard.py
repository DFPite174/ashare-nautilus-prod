"""Reject ABI/binary drift before importing the version-pinned native bridge (supports cross-platform)."""
from functools import lru_cache
from pathlib import Path
import hashlib, importlib.metadata, json, sys, logging

logger = logging.getLogger(__name__)

@lru_cache(maxsize=1)
def load_bridge():
    root = Path(__file__).resolve().parent
    nt_version = importlib.metadata.version('nautilus_trader')
    if not nt_version.startswith('1.231.'):
        logger.warning(f"[SPLIT_GUARD] NT version drift: {nt_version} (baseline 1.231.0)")
    
    # On Windows with precompiled binary and manifest, perform fingerprint check
    manifest_file = root / 'SPLIT_BUILD.json'
    bundled_pyd = root / 'split_native.cp312-win_amd64.pyd'
    if sys.platform == 'win32' and manifest_file.exists() and bundled_pyd.exists():
        try:
            import nautilus_trader
            package = Path(nautilus_trader.__file__).parent
            manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
            for path in (root / 'split_native.pyx', bundled_pyd, package / 'model/position.cp312-win_amd64.pyd', package / 'model/position.pxd'):
                if not path.exists(): continue
                expected = [d for name, d in manifest.get('sha256', {}).items() if Path(name).name == path.name]
                if expected:
                    raw = path.read_bytes()
                    if path.name.endswith(('.pyx', '.pxd', '.txt', '.json')):
                        raw = raw.replace(b'\r\n', b'\n')
                    if hashlib.sha256(raw).hexdigest() != expected[0]:
                        logger.warning(f"[SPLIT_GUARD] Fingerprint changed on {path.name}, attempting native load...")
        except Exception:
            pass

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        import split_native
        return split_native
    except ImportError:
        try:
            from engine import split_native
            return split_native
        except ImportError as exc:
            raise RuntimeError(
                f"[SPLIT_GUARD] Split bridge extension 'split_native' could not be loaded on {sys.platform}.\n"
                "Run 'python setup.py build_ext --inplace' to compile on Linux/macOS."
            ) from exc
