"""Évaluation : python -m tfproject.evaluate --model models/model.keras"""
import argparse
from pathlib import Path

import tensorflow as tf

from tfproject.data import load_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=Path("models/model.keras"))
    args = parser.parse_args()

    _, (x_test, y_test) = load_data()
    model = tf.keras.models.load_model(args.model)
    loss, acc = model.evaluate(x_test, y_test, verbose=0)
    print(f"loss={loss:.4f} accuracy={acc:.4f}")


if __name__ == "__main__":
    main()
