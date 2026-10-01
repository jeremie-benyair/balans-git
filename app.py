"""Application web de démonstration. Lancement : streamlit run app.py"""
from pathlib import Path

import numpy as np
import streamlit as st
import tensorflow as tf
from PIL import Image, ImageOps

MODEL_PATH = Path("models/model.keras")

st.title("Reconnaissance de chiffres manuscrits")

if not MODEL_PATH.exists():
    st.error("Modèle introuvable. Lancez d'abord : python -m tfproject.train")
    st.stop()


@st.cache_resource
def load_model():
    return tf.keras.models.load_model(MODEL_PATH)


model = load_model()

file = st.file_uploader("Importez une image d'un chiffre", type=["png", "jpg", "jpeg"])
invert = st.checkbox("Inverser les couleurs (chiffre foncé sur fond clair)", value=True)

if file:
    img = Image.open(file).convert("L")
    st.image(img, caption="Image importée", width=150)
    if invert:
        img = ImageOps.invert(img)
    x = np.array(img.resize((28, 28)), dtype=np.float32) / 255.0
    probs = model.predict(x[np.newaxis, ...], verbose=0)[0]
    st.subheader(f"Prédiction : {int(np.argmax(probs))}")
    st.bar_chart(probs)
