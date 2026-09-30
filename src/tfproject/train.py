"""Entraînement : python -m tfproject.train --epochs 5"""
import argparse
from pathlib import Path

from tfproject.data import load_data
from tfproject.model import build_model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--output", type=Path, default=Path("models/model.keras"))
    args = parser.parse_args()

    (x_train, y_train), (x_test, y_test) = load_data()
    model = build_model()
    model.fit(
        x_train,
        y_train,
        epochs=args.epochs,
        batch_size=args.batch_size,
        validation_data=(x_test, y_test),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    model.save(args.output)
    print(f"Modèle sauvegardé dans {args.output}")


if __name__ == "__main__":
    main()
