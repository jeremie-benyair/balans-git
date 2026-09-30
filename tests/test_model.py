import numpy as np

from tfproject.model import build_model


def test_model_output_shape():
    model = build_model(num_classes=10)
    x = np.random.rand(4, 28, 28).astype("float32")
    y = model.predict(x, verbose=0)
    assert y.shape == (4, 10)
    assert np.allclose(y.sum(axis=1), 1.0, atol=1e-4)
