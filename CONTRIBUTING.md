# Workflow d'équipe

1. Ne jamais pousser directement sur `main`.
2. Créer une branche par tâche : `git checkout -b feature/nom-court`.
3. Commits petits et clairs.
4. Ouvrir une **Merge Request** ; un coéquipier relit avant de fusionner.
5. Le pipeline CI (lint + tests) doit être au vert avant la fusion.
6. Données et modèles lourds : hors de Git (DVC, Drive, Hugging Face Hub).
