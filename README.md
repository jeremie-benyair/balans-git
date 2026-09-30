# Balans

Conception de l'application Balans

## Installation

```bash
python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -r requirements-dev.txt
pip install -e .
```

## Utilisation

```bash
python -m tfproject.train --epochs 5
python -m tfproject.evaluate
pytest
```

## Structure

- `src/tfproject/` : code (données, modèle, entraînement, évaluation)
- `tests/` : tests automatisés
- `notebooks/` : exploration (Colab / Jupyter)
- `data/`, `models/` : ignorés par Git
- `.gitlab-ci.yml` : pipeline CI

Voir `CONTRIBUTING.md` pour le workflow d'équipe.
