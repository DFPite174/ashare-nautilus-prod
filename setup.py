"""
Cross-platform build configuration for ashare-nautilus-backtest-engine.
- Windows: Uses shipped precompiled platform binary when available.
- Linux/macOS: Compiles split_native.pyx via Cython linking NautilusTrader C-API.
"""
import sys
from pathlib import Path
from setuptools import setup, Extension, Distribution

HAVE_CYTHON = False
try:
    from Cython.Build import cythonize
    HAVE_CYTHON = True
except ImportError:
    pass

ext_modules = []
needs_compile = ("build_ext" in sys.argv) or (sys.platform != "win32")

if HAVE_CYTHON and needs_compile:
    try:
        import nautilus_trader
        nt_dir = Path(nautilus_trader.__file__).parent
        compile_args = ["-O3"] if sys.platform != "win32" else ["/O2"]
        ext_modules = cythonize([
            Extension(
                name="engine.split_native",
                sources=["engine/split_native.pyx"],
                include_dirs=[
                    str(nt_dir),
                    str(nt_dir / "model"),
                    str(Path(__file__).parent / "engine"),
                ],
                language="c++",
                extra_compile_args=compile_args,
            )
        ], compiler_directives={"language_level": "3", "boundscheck": False, "wraparound": False})
    except Exception as e:
        print(f"Notice: Cython extension configuration skipped: {e}")

class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True

if ext_modules:
    setup(ext_modules=ext_modules)
else:
    setup(distclass=BinaryDistribution)
