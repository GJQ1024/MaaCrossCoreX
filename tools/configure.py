from pathlib import Path

REQUIRED_OCR_FILES = ("det.onnx", "rec.onnx", "keys.txt")

assets_dir = Path(__file__).parent.parent.resolve() / "assets"


def configure_ocr_model():
    """校验仓库内的 OCR 模型。

    仓库里的 assets/resource/model/ocr 是准绳（与已发布包一致）。这里不再从
    MaaCommonAssets/OCR 复制另一套模型：那套与发布包用的模型不同，静默复制等于
    悄悄换模型。缺文件时直接失败，提示从仓库恢复。
    """
    ocr_dir = assets_dir / "resource" / "model" / "ocr"
    missing = [name for name in REQUIRED_OCR_FILES if not (ocr_dir / name).is_file()]
    if missing:
        print(f"OCR 模型缺失: {', '.join(missing)}（目录 {ocr_dir}）")
        print("请从仓库恢复 assets/resource/model/ocr 下的模型文件；")
        print("不要用 assets/MaaCommonAssets/OCR 下的模型替代，那套与发布包不一致。")
        exit(1)

    print(f"Found OCR model in repository, using it: {ocr_dir}")


if __name__ == "__main__":
    configure_ocr_model()

    print("OCR model configured.")
