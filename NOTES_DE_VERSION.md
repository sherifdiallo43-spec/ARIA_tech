# Notes de version ARIA_point

Une section par version, la plus récente en haut. Le texte de chaque version installée
s'affiche à l'école à la fin de la mise à jour : l'écrire pour le directeur, pas pour
le développeur. `pt creer-paquet` refuse de fabriquer un paquet sans sa section.

## 1.1.0 (2026-10-25)

Élèves qui quittent l'école et rentrée scolaire.

- Un élève qui quitte l'école est marqué « parti » : ses parents ne reçoivent plus d'alerte
  d'absence et il sort des rapports. Son historique est conservé.
- À la rentrée, la liste complète des élèves de la nouvelle année met à jour les classes,
  ajoute les nouveaux et repère ceux qui sont partis. Aucune empreinte à refaire pour les
  élèves qui restent.
- Les empreintes des élèves partis peuvent être retirées des pointeuses.
- Le numéro d'un ancien élève n'est jamais redonné à un nouveau.

## 1.0.0 (2026-10-25)

Première version numérotée d'ARIA_point.

- Pointage des élèves et du personnel, alertes WhatsApp aux parents (formules Essentiel et Complet).
- Rapports PDF du jour et du mois, rapport confidentiel du personnel envoyé au directeur.
- Plusieurs pointeuses par école, copie des empreintes d'un boîtier à l'autre.
- Sauvegardes quotidiennes, copie sur clé USB.
- Mises à jour sûres : la base est mise de côté avant chaque mise à jour, et tout est remis
  comme avant en cas de problème.
