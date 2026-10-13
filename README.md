# ARIA_point

Produit d'**ARIA_tech** : système de pointage scolaire.

Passerelle entre une pointeuse biométrique **ZKTeco** et les parents d'élèves :
chaque empreinte posée à l'entrée de l'école déclenche un message **WhatsApp** au
parent, si son abonnement (1 000 FCFA/mois après un essai gratuit) est actif.

Le logiciel tourne sur un PC Windows de l'école, branché sur le même réseau que la pointeuse.

## Ce qu'il fait

| Fonction | Comment |
|---|---|
| Lecture de la pointeuse | Toutes les 30 s, en TCP/IP (port 4370), via `pyzk` |
| Zéro doublon | Chaque pointage (élève + heure exacte) n'est enregistré qu'une fois, même si on relit la mémoire du boîtier 100 fois |
| Coupure réseau ou PC éteint | La pointeuse garde les empreintes en mémoire. À la reconnexion, tout est rattrapé automatiquement |
| Coupure internet | Les messages attendent dans une file et partent au retour d'internet. Après 2 h, ils sont abandonnés (inutile d'annoncer à 15 h une arrivée de 7 h 30) |
| Abonnements | Essai gratuit puis paiements saisis à la main. Sans abonnement actif : pas de message |
| Deux pointages par jour | Le premier pointage de la journée est l'arrivée, le suivant la sortie (fin des cours ou départ anticipé). Deux pointages à moins de 30 min = un seul |
| Retards | Arrivée après l'heure `retard_apres` de `config.toml` (8 h par défaut) |
| Sauvegarde | Copie de la base toutes les 24 h dans `backups/`, gardée 30 jours |
| Rapport PDF du jour | Chaque soir à partir de 18 h, dans `rapports/journaliers/` : présents, **absents (avec le téléphone du parent)**, retards, pointages incomplets (arrivée sans sortie ou l'inverse) |
| Rapport PDF du mois | Le 1er du mois suivant, dans `rapports/mensuels/` : assiduité par classe, élèves à surveiller, alertes WhatsApp |
| Relevé du prestataire | Le 1er du mois suivant, dans `rapports/prestataire/` : abonnements, détail des paiements, montant dû au prestataire, parents à relancer |
| Fin d'année | Archivage des pointages dans `archives/` et nettoyage de la base, fiches élèves conservées |

## Tableau de bord web

Une page web, protégée par mot de passe, pour suivre la journée sans ligne de commande :
**Aujourd'hui** (présents, absents avec le téléphone du parent, retards, derniers pointages,
actualisation toutes les 30 s, navigation vers les jours précédents) et **Rapports**
(télécharger ou générer les PDF du jour et du mois).

```powershell
# 1. Choisis un mot de passe (8 caractères minimum) dans le fichier .env :
#    DASHBOARD_PASSWORD=ton-mot-de-passe
# 2. Lance le tableau de bord (en plus de `pt run`, dans un second terminal) :
pt web
# 3. Ouvre http://localhost:8000 dans le navigateur
```

Par défaut, seul ce PC peut l'ouvrir. `pt web --reseau` le rend accessible depuis les autres
appareils du réseau de l'école (téléphone du directeur, par exemple) : choisis alors un mot de
passe solide, car les noms d'élèves et les téléphones des parents sont des données personnelles.
Le relevé du prestataire n'est volontairement pas visible dans cette interface.

## Installation (Windows)

Prérequis : Python 3.11 ou plus récent. Sur ce PC, la commande à utiliser est `py`
(la commande `python` ouvre le Microsoft Store).

```powershell
cd C:\Users\DELL\Documents\pointage-scolaire

# 1. Environnement virtuel : les bibliothèques du projet restent isolées du reste du PC
py -m venv .venv

# 2. Installation des bibliothèques (pyzk, twilio, fpdf2, flask, waitress...)
.venv\Scripts\python -m pip install -r requirements.txt

# 3. Fichier des secrets
copy .env.example .env

# 4. Vérification : tous les tests doivent passer
.venv\Scripts\python -m pytest
```

Dans la suite, `.venv\Scripts\python main.py` est abrégé en **`pt`**. Tape la commande complète.

## Essai rapide sans pointeuse (démo)

`config.toml` est livré en mode `simulee` (fausse pointeuse) et `console` (les messages
s'affichent à l'écran au lieu de partir sur WhatsApp).

```powershell
pt init                                         # crée la base data/pointage.db
pt import-eleves exemples\eleves_exemple.csv    # 9 élèves (la ligne 11 est refusée exprès : numéro invalide)
pt simuler --n 6 --heure 07:40                  # 6 élèves arrivent à 7 h 40
pt simuler --n 2 --heure 08:25                  # 2 élèves arrivent en retard
pt sync                                         # lecture : messages aux parents (à l'écran)
pt simuler --n 5 --heure 17:10                  # 5 élèves sortent à la fin des cours
pt sync
pt rapport                                      # rapport PDF du jour dans rapports/journaliers/
pt rapport --mois 2026-10                       # rapport PDF du mois dans rapports/mensuels/
pt statut
```

Un pointage récupéré plus de 2 h après coup n'envoie pas de message (`age_max_minutes`).
Pour voir les messages, simule une heure proche de l'heure actuelle ou à venir.

## Toutes les commandes

| Commande | Rôle |
|---|---|
| `pt init` | Crée la base de données |
| `pt import-eleves fichier.csv` | Ajoute ou met à jour les élèves (réimporter ne redonne pas d'essai gratuit) |
| `pt paiement 12 --mois 3` | Enregistre un paiement de 3 mois pour l'élève n° 12 de la pointeuse |
| `pt sync` | Une seule lecture de la pointeuse, plus l'envoi des messages en attente |
| `pt run` | Le service en continu (c'est ce qui tourne à l'école) |
| `pt web [--port N] [--reseau]` | Le tableau de bord web (mot de passe dans `.env`) |
| `pt simuler --jours N` / `--n N [--heure HH:MM]` | Ajoute des pointages dans la fausse pointeuse |
| `pt rapport [--jour AAAA-MM-JJ]` | Rapport PDF d'une journée (par défaut : aujourd'hui) |
| `pt rapport --mois AAAA-MM` | Rapport PDF d'un mois |
| `pt releve [--mois AAAA-MM]` | Relevé du prestataire : paiements, montant dû, parents à relancer (par défaut : le mois précédent) |
| `pt sauvegarder` | Sauvegarde immédiate |
| `pt archiver --annee 2026-2027` | Fin d'année (voir plus bas) |
| `pt vider-pointeuse` | Met tout en sécurité puis vide la mémoire du boîtier |
| `pt statut` | Résumé : élèves, abonnements, pointages du jour, messages en attente, mémoire du boîtier |
| `pt utilisateurs` | Liste les utilisateurs enregistrés dans la pointeuse |

## Fichier des élèves (CSV)

À préparer dans Excel puis à enregistrer en « CSV (séparateur : point-virgule) » :

```
zk_user_id;nom;prenom;classe;tel_parent
1;Diop;Awa;6e A;77 123 45 67
```

- `zk_user_id` : le **numéro sous lequel l'empreinte de l'élève est enregistrée dans la pointeuse**.
  C'est le lien entre le boîtier et la base. `pt utilisateurs` affiche ces numéros.
- `tel_parent` : accepté sous toutes les formes courantes (`77 123 45 67`, `00221...`, `+221...`).
  Il est converti au format international `+221771234567`.

## Brancher la vraie pointeuse ZKTeco

1. Sur la pointeuse (menu Communication / Réseau) : donne-lui une **adresse IP fixe**,
   par exemple `192.168.1.201`, sur le même réseau que le PC.
2. Enregistre l'empreinte de chaque élève sous son numéro (`zk_user_id`).
3. Dans `config.toml` : `mode = "zk"` et l'adresse `ip`.
4. Si un mot de passe de communication (Comm Key) est réglé sur le boîtier, mets-le dans `.env` (`ZK_MOT_DE_PASSE`).
5. Test : `pt utilisateurs`, puis `pt sync`.

Si la connexion échoue avec un vieux modèle, essaie `forcer_udp = true`.

### Mémoire du boîtier

La pointeuse garde chaque pointage dans une mémoire limitée (souvent 50 000 à 200 000).
300 élèves qui pointent 4 fois par jour en produisent environ 25 000 par trimestre. Le
logiciel la vide donc **automatiquement** quand elle dépasse `vider_au_dela` (20 000 par
défaut), dans la plage horaire `vidage_auto_entre` (18 h - 23 h 59 par défaut).

Avant d'effacer quoi que ce soit, il bloque le boîtier quelques secondes, lit tout, vérifie
que chaque pointage est dans la base, puis fait une sauvegarde de la base et une copie brute
de la mémoire (`backups/memoire_pointeuse_...json`). Si une seule de ces étapes échoue,
rien n'est effacé.

Vidage manuel : `pt vider-pointeuse`. La taille actuelle de la mémoire s'affiche avec `pt statut`.

## Brancher WhatsApp (Twilio)

**Pour tester (bac à sable, gratuit) :**
1. Crée un compte sur https://www.twilio.com et récupère l'Account SID et l'Auth Token.
2. Console Twilio → Messaging → Try it out → WhatsApp : chaque téléphone de test doit envoyer
   le code `join ...` indiqué au numéro du bac à sable.
3. Remplis `.env` (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`) et mets `mode = "twilio"` dans `config.toml`.

**Pour la production (de vrais parents) :**
WhatsApp interdit d'écrire en texte libre à quelqu'un qui ne vous a pas écrit dans les
dernières 24 h. Il faut :
1. Un **numéro WhatsApp Business** enregistré via Twilio (vérification de l'entreprise par Meta).
2. Un **modèle de message** (Content Template) de catégorie « Utility », approuvé par Meta, avec 4 variables :
   `{{1}} : {{2}} {{3}} à {{4}}.` (école, élève, événement, heure).
   Exemple de rendu : *Groupe Scolaire Exemple : Awa Diop est arrivé(e) à l'école à 07h42.*
3. Son identifiant (commence par `HX`) dans `.env` → `TWILIO_CONTENT_SID`.

À savoir : « envoyé » signifie « accepté par Twilio ». La confirmation de lecture n'est pas suivie dans cette version.

## Lancer le service automatiquement au démarrage du PC

Planificateur de tâches Windows → Créer une tâche :
- Déclencheur : « À l'ouverture de session »
- Action : programme `C:\Users\DELL\Documents\pointage-scolaire\.venv\Scripts\python.exe`,
  arguments `main.py run`, démarrer dans `C:\Users\DELL\Documents\pointage-scolaire`
- Paramètres : « Si la tâche échoue, redémarrer toutes les 1 minute »

Le journal de fonctionnement est dans `logs/pointage.log`.

## Fin d'année scolaire

Pendant les vacances, après la dernière journée de cours :

```powershell
pt archiver --annee 2026-2027              # archive tout jusqu'au 31/07/2027
pt archiver --annee 2026-2027 --fin 2027-07-15
```

Le logiciel fait d'abord une sauvegarde complète, copie l'année dans
`archives/annee_2026-2027.db` (et en CSV lisibles dans Excel), vérifie la copie, puis
seulement vide les pointages, messages et paiements de la base principale. Les
fiches élèves et leurs dates d'abonnement sont conservées. Les vieux pointages encore
présents dans la mémoire de la pointeuse ne seront pas réimportés.

## Organisation du code

```
main.py                  ligne de commande
pointage/config.py       lecture de config.toml et .env
pointage/db.py           schéma SQLite et utilitaires de dates
pointage/device.py       vraie pointeuse (pyzk) et pointeuse simulée
pointage/collector.py    lecture, anti-doublon, classement arrivée/sortie/retard
pointage/eleves.py       fiches élèves, import CSV, numéros de téléphone
pointage/subscriptions.py essai gratuit, paiements, abonnement actif ou non
pointage/notifier.py     file d'attente et envoi WhatsApp (Twilio ou console)
pointage/backup.py       sauvegardes et rotation
pointage/purge.py        vidage sécurisé de la mémoire du boîtier
pointage/reports.py      statistiques mensuelles et PDF
pointage/archive.py      archivage de fin d'année
pointage/service.py      boucle du service continu
pointage/web.py          tableau de bord web (Flask) ; pages dans pointage/templates/
tests/                   tests automatiques (pytest)
```

## Limites connues (V1)

- Un élève qui arrive très tard (après 16 h, `sortie_seule_apres`) sans avoir pointé le
  matin est compté comme une sortie : le logiciel suppose qu'il a oublié de pointer en arrivant.
- Un élève absent un jour sans aucun pointage dans toute l'école (grève, jour férié
  imprévu) n'est pas compté absent : ce jour n'est pas considéré comme un jour d'école.
- Une seule pointeuse par école.
- Paiements saisis à la main (pas encore de Wave ou d'Orange Money).

## Données personnelles

Les empreintes digitales restent **dans la pointeuse**. Le logiciel ne reçoit qu'un
numéro et une heure. La base contient cependant des données personnelles de mineurs
(noms, présences, téléphones des parents) : au Sénégal, leur traitement relève de la
loi n° 2008-12 et doit être déclaré à la CDP (Commission de protection des Données
Personnelles). Le consentement écrit des parents est à recueillir à l'inscription.
