"""Chargement et préparation des données (exemple : MNIST)."""
import numpy as np
import tensorflow as tf


def load_data():
    """Retourne (x_train, y_train), (x_test, y_test) normalisés entre 0 et 1."""
    (x_train, y_train), (x_test, y_test) = tf.keras.datasets.mnist.load_data()
    x_train = x_train.astype(np.float32) / 255.0
    x_test = x_test.astype(np.float32) / 255.0
    return (x_train, y_train), (x_test, y_test)
