"""Package the already downloaded, pinned ONNX candidate for offline transfer."""
import hashlib
import argparse
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.security.service import _RULES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-only", action="store_true", help="Reuse an existing model on the target machine")
    args = parser.parse_args()
    model_dir = ROOT / "tmp/privacy-model-mini"
    model = model_dir / "onnx/model_quantized.onnx"
    expected = "80db3cf7b9edc7d1ba4cc20561a46efda957b1f07a27ac911d31304be8baf5f3"
    if not args.code_only and hashlib.sha256(model.read_bytes()).hexdigest() != expected:
        raise ValueError("Unexpected model revision")
    files = {
        "scripts/privacy_ner_eval.py": (ROOT / "scripts/privacy_ner_eval.py").read_bytes(),
        "tests/fixtures/privacy_ner_cases.json": (ROOT / "tests/fixtures/privacy_ner_cases.json").read_bytes(),
        "rules.json": json.dumps([{"pattern": r._regex.pattern, "flags": r._regex.flags} for r in _RULES], ensure_ascii=False).encode("utf-8"),
        "requirements.txt": b"onnxruntime==1.22.1\nnumpy==1.26.4\ntokenizers==0.22.2\npsutil>=5.9,<8\n",
        "README.md": (ROOT / ("docs/PRIVACY_CONTEXT_V2.md" if args.code_only else "docs/PRIVACY_CPU_PROBE.md")).read_bytes(),
        "LICENSE": (ROOT / "LICENSE").read_bytes(),
    }
    if not args.code_only:
        for name in ("config.json", "tokenizer.json", "onnx/model_quantized.onnx"):
            files["model/" + name] = (model_dir / name).read_bytes()
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files["SHA256.json"] = json.dumps(manifest, indent=2).encode("utf-8")
    destination = ROOT / ("artifacts/privacy-cpu-probe-context-v2.zip" if args.code_only else "artifacts/privacy-cpu-probe.zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("Corrupt archive")
    print(destination)
    print("bytes", destination.stat().st_size)


if __name__ == "__main__":
    main()
