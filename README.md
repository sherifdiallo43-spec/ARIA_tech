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
# 1. Choisis un mot de passe (12 caractères minimum) dans le fichier .env :
#    DASHBOARD_PASSWORD=ton-mot-de-passe
# 2. Lance le tableau de bord (en plus de `pt run`, dans un second terminal) :
pt web
# 3. Ouvre http://localhost:8000 dans le navigateur
```

Par défaut, seul ce PC peut l'ouvrir. `pt web --reseau` le rend accessible depuis les autres
appareils du réseau de l'école (téléphone du directeur, par exemple) : choisis alors un mot de
passe solide, car les noms d'élèves et les téléphones des parents sont des données personnelles.
Le relevé du prestataire n'est volontairement pas visible dans cette interface.

## Sécurité : à faire dans chaque école

Le logiciel manipule des données d'enfants (noms, présences, téléphones des parents).
Le code se protège lui-même (mot de passe, blocage après 5 essais faux pendant 15 minutes,
en-têtes de sécurité, aucune donnée ni secret envoyé sur GitHub), mais l'installation compte autant :

1. **Chiffrer le disque du PC** avec BitLocker (Paramètres Windows > Confidentialité et
   sécurité > Chiffrement de l'appareil). Sans ça, un PC volé donne accès à toutes les données.
2. **Un mot de passe long** pour le tableau de bord (`DASHBOARD_PASSWORD`, 12 caractères
   ou plus), différent pour chaque école, et une session Windows protégée par mot de passe.
3. **Ne pas utiliser `--reseau` sans HTTPS** : sur le réseau, le mot de passe circule en clair.
   Par défaut, le tableau de bord n'est ouvert qu'au PC lui-même.
4. **Protéger la pointeuse** : définir sa clé de communication (Comm Key, à reporter dans
   `ZK_MOT_DE_PASSE`) et, si possible, la brancher sur un réseau séparé du Wi-Fi des visiteurs.
   Son protocole n'est pas chiffré.
5. **Ne jamais copier `.env`** ailleurs que sur ce PC, et ne pas partager le dossier `data/`.
   Le jeton WhatsApp (`META_JETON`) permet d'envoyer des messages au nom d'ARIA_point :
   si un PC d'école est volé, révoque-le aussitôt (voir « Brancher WhatsApp »).
6. **Clé USB de sauvegarde chiffrée** (voir « Sauvegardes sur clé USB ») : elle contient les
   noms et les téléphones des parents. Perdue sans chiffrement, ce serait une fuite de données.
7. **Informer les parents** que le nom de l'enfant transite par WhatsApp, de Meta (serveurs
   à l'étranger) quand les alertes sont activées, et le mentionner dans la déclaration à la CDP.

## Installation (Windows)

Prérequis : Python 3.11 ou plus récent. Sur ce PC, la commande à utiliser est `py`
(la commande `python` ouvre le Microsoft Store).

```powershell
cd C:\Users\DELL\Documents\pointage-scolaire

# 1. Environnement virtuel : les bibliothèques du projet restent isolées du reste du PC
py -m venv .venv

# 2. Installation des bibliothèques (pyzk, fpdf2, flask, waitress...)
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
| `pt rentree fichier.csv [--date J]` | Nouvelle année : liste complète, les absents de la liste sont marqués partis |
| `pt eleve-parti 45 [46 ...] [--date J]` | Un élève quitte l'école : plus d'alerte d'absence |
| `pt personnel-parti 901 [--date J]` | Un membre du personnel quitte l'école : il sort du rapport du directeur |
| `pt nettoyer-boitier` | Efface des pointeuses les élèves et le personnel partis, et leurs empreintes |
| `pt import-personnel fichier.csv` | Ajoute ou met à jour les profs et le personnel |
| `pt import-emploi-du-temps fichier.csv` | Emploi du temps du personnel (une ligne par cours) |
| `pt rapport-personnel [--jour J \| --mois M] [--envoyer]` | Rapport du personnel pour le directeur, et envoi par email |
| `pt test-email ADRESSE` | Vérifie l'envoi des emails |
| `pt paiement 12 --mois 3` | Enregistre un paiement de 3 mois pour l'élève n° 12 de la pointeuse |
| `pt sync` | Une seule lecture de la pointeuse, plus l'envoi des messages en attente |
| `pt run` | Le service en continu (c'est ce qui tourne à l'école) |
| `pt test-whatsapp NUMERO [--hello]` | Envoie une alerte WhatsApp de test via Meta (`--hello` : le modèle hello_world de Meta, pour vérifier le jeton) |
| `pt web [--port N] [--reseau]` | Le tableau de bord web (mot de passe dans `.env`) |
| `pt simuler --jours N` / `--n N [--profs N] [--heure HH:MM]` | Ajoute des pointages dans la fausse pointeuse |
| `pt rapport [--jour AAAA-MM-JJ]` | Rapport PDF d'une journée (par défaut : aujourd'hui) |
| `pt rapport --mois AAAA-MM` | Rapport PDF d'un mois |
| `pt releve [--mois AAAA-MM]` | Relevé du prestataire : paiements, montant dû, parents à relancer (par défaut : le mois précédent) |
| `pt sauvegarder` | Sauvegarde immédiate |
| `pt archiver --annee 2026-2027` | Fin d'année (voir plus bas) |
| `pt vider-pointeuse` | Met tout en sécurité puis vide la mémoire du boîtier |
| `pt statut` | Résumé : élèves, abonnements, pointages du jour, messages en attente, mémoire du boîtier |
| `pt utilisateurs` | Liste les utilisateurs enregistrés dans la pointeuse |
| `pt verifier-numeros` | Compare les numéros du boîtier avec la base (doigts manquants, fautes de frappe) |
| `pt copier-empreintes [--source NOM]` | Recopie les personnes et empreintes d'un boîtier vers les autres (voir plus bas) |
| `pt version` | Version du logiciel et de la base |
| `pt mettre-a-jour PAQUET.zip` | Installe une nouvelle version (voir « Mises à jour ») |
| `pt annuler-mise-a-jour` | Revient à la version d'avant la dernière mise à jour |
| `pt verifier-installation` | Contrôle que le logiciel tourne avec la configuration et la base de l'école |
| `pt creer-paquet` | (PC de développement) Fabrique le paquet de mise à jour dans `dist/` |

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
- Un numéro n'est **jamais redonné** : celui d'un élève parti reste à lui (sinon son historique
  passerait au nouveau, et ses doigts sont peut-être encore sur le boîtier). `pt statut` affiche
  le prochain numéro libre.

## Élèves qui partent, et rentrée scolaire

Le boîtier ne connaît que des **numéros** et les empreintes liées. Les classes, noms et
téléphones sont dans la base, sur le PC. Un élève qui change de classe ne refait donc **pas**
ses empreintes : on met seulement sa classe à jour dans la base.

**Un élève quitte l'école en cours d'année** : `pt eleve-parti 45` (ou `--date 2026-11-02` pour
le premier jour où il n'est plus élève). Ses parents ne reçoivent plus d'alerte d'absence et il
sort des rapports à partir de cette date. Rien n'est effacé : une erreur se corrige en
réimportant l'élève, qui est réactivé.

**À chaque rentrée**, avec la liste **complète** des élèves de la nouvelle année (même format CSV) :

```powershell
pt archiver --annee 2026-2027               # pendant les vacances (voir « Fin d'année scolaire »)
pt rentree liste_2027-2028.csv --date 2027-10-04
```

- Les élèves de la liste déjà connus sont mis à jour (classe, téléphone), leurs empreintes restent.
- Les nouveaux sont créés : enregistre leurs doigts sous leur numéro, puis `pt verifier-numeros`.
- Les anciens élèves qui reviennent (même numéro, même nom) sont réactivés.
- Les élèves inscrits mais **absents de la liste** sont marqués partis.

La commande montre d'abord ce qui va changer (nombre de changements de classe, nouveaux,
**liste des partants**), puis demande `RENTREE`. Par sécurité, **rien n'est appliqué** si une
ligne du fichier est en erreur (son élève passerait pour parti), ni si plus de la moitié des
élèves partiraient (fichier d'une seule classe ?) sans `--forcer`.

**Retirer les partis des pointeuses** : `pt nettoyer-boitier`. Il efface de chaque boîtier les
élèves et membres du personnel marqués partis, avec leurs empreintes, puis relit le boîtier pour
vérifier. Il ne touche ni aux personnes encore là, ni aux numéros inconnus de la base (un
administrateur du boîtier, par exemple). Utile pour la place (un F18 annonce environ 3 000 empreintes). À faire hors des heures
de passage : chaque boîtier est bloqué quelques instants.

## Personnel et emploi du temps (facultatif)

Pour les écoles qui veulent aussi suivre les profs. Sans personnel inscrit, rien ne change.

**1. Le personnel** (`pt import-personnel`, voir `exemples/personnel_exemple.csv`) :

```
zk_user_id;nom;prenom;fonction
900;Ndiaye;Mamadou;Prof de maths
```

Réserve une plage de numéros de pointeuse au personnel (900 et plus, par exemple). Un même
numéro ne peut pas être à la fois un élève et un membre du personnel.

**2. L'emploi du temps** (`pt import-emploi-du-temps`, voir `exemples/emploi_du_temps_exemple.csv`),
une ligne par cours, du lundi au samedi :

```
zk_user_id;jour;debut;fin;classe;matiere
900;lundi;08:00;10:00;3e A;Maths
900;lundi;15:00;17:00;4e A;Maths
```

Réimporter remplace l'emploi du temps des personnes présentes dans le fichier (les autres
gardent le leur). Une personne avec une ligne en erreur garde son ancien emploi du temps.

**Ce que le logiciel en tire**

- Pointages : arrivée, sortie, arrivée, sortie... autant que nécessaire.
- Chaque cours est **à l'heure**, **en retard** (arrivée plus de `tolerance_retard_minutes`
  après le début, 5 par défaut) ou **manqué** (personne absente pendant tout le cours).
- Un jour sans cours n'est jamais compté comme absence.
- Heures = temps de présence (arrivée → sortie). Une sortie oubliée n'est pas comptée.
- Aucun message WhatsApp, aucun abonnement pour le personnel.

**Un membre du personnel quitte l'école** : `pt personnel-parti 901` (ou `--date 2026-11-02`,
premier jour où il n'en fait plus partie). Il sort du rapport du directeur à partir de cette
date, sinon ses cours y seraient « manqués » chaque jour. Le rapport du mois de son départ le
garde, avec ses heures jusqu'au départ (utile pour la paie). Rien n'est effacé, et le
réimporter le réactive. Son numéro n'est jamais redonné à quelqu'un d'autre.

## Rapport du personnel pour le directeur

Le rapport du personnel est **confidentiel** : il est séparé du rapport des élèves, rangé dans
`rapports/personnel/`, jamais affiché sur le tableau de bord, et envoyé **par email au directeur**
(chaque jour après 18 h, et le 1er du mois pour le mois écoulé). Sans internet, l'envoi repart
l'heure suivante, et un rapport n'est jamais envoyé deux fois.

Dans `config.toml` :

```toml
[directeur]
email = "directeur@ecole.sn"
mode = "smtp"        # "console" pour les démos : l'email s'affiche sans partir
```

Dans `.env` (avec Gmail : active la validation en 2 étapes, puis crée un **mot de passe
d'application** dans myaccount.google.com > Sécurité ; ce n'est pas le mot de passe du compte) :

```
SMTP_UTILISATEUR=aria.tech.rapports@gmail.com
SMTP_MOT_DE_PASSE=xxxx xxxx xxxx xxxx
```

Puis vérifie : `pt test-email ton-adresse@gmail.com`.

Les empreintes du personnel sont aussi des données biométriques : à inclure dans la
déclaration à la CDP, avec le consentement écrit de chaque membre.

## Brancher la vraie pointeuse ZKTeco

1. Sur la pointeuse (menu Communication / Réseau) : donne-lui une **adresse IP fixe**,
   par exemple `192.168.1.201`, sur le même réseau que le PC.
2. Enregistre l'empreinte de chaque élève sous son numéro (`zk_user_id`).
3. Dans `config.toml` : `mode = "zk"` et l'adresse `ip`.
4. Si un mot de passe de communication (Comm Key) est réglé sur le boîtier, mets-le dans `.env` (`ZK_MOT_DE_PASSE`).
5. Test : `pt utilisateurs`, puis `pt sync`.

Si la connexion échoue avec un vieux modèle, essaie `forcer_udp = true`.

### Plusieurs pointeuses : enregistrer les doigts une seule fois

Un élève doit pouvoir pointer à n'importe quelle entrée, donc ses empreintes doivent être
sur **tous** les boîtiers. Inutile de l'enregistrer 3 fois :

1. Enregistre tous les doigts sur **un seul** boîtier (le premier de `config.toml`).
2. Branche les autres sur le réseau, chacun avec son bloc `[[pointeuses]]`.
3. `pt copier-empreintes` : lit le premier boîtier, recopie chaque personne et ses empreintes
   sur les autres, puis **relit** chaque boîtier pour vérifier que rien ne manque.
   Si les doigts ont été enregistrés ailleurs : `pt copier-empreintes --source "Portail sud"`.

- Les personnes déjà présentes sont mises à jour, celles qui ne sont que sur le boîtier cible
  ne sont pas touchées. La copie peut être relancée sans risque (après de nouvelles inscriptions).
- Chaque boîtier est **bloqué** pendant la copie (plusieurs minutes pour 1 000 élèves) :
  à faire le soir ou le week-end, jamais aux heures de passage.
- Si un boîtier n'a pas la place, rien n'y est écrit et la commande le dit.
- Utilise **le même modèle** partout : le format des empreintes dépend du boîtier.
- Avant la première école, teste sur 2 vrais boîtiers qu'un doigt copié est bien reconnu.

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

## Quels messages envoyer aux parents (formules)

Dans `config.toml`, section `[notifications]`, la liste `types_notifies` choisit les messages :

| Formule | `types_notifies` | Le parent reçoit un message... |
|---|---|---|
| Complet | `["arrivee", "sortie", "absence"]` | à chaque arrivée et sortie, et si l'enfant n'a pas pointé |
| Essentiel | `["retard", "absence"]` | seulement en cas de retard ou d'absence |

**L'alerte d'absence** part à `absence_apres` (9 h par défaut), du lundi au samedi, pour chaque
élève qui n'a pas pointé : « Groupe Scolaire X : Awa Diop (6e A) n'avait pas pointé à l'école à
09h00. » Un élève qui arrive ensuite déclenche en plus le message de retard.

Comme un message d'absence envoyé à tort inquiète beaucoup un parent, **rien ne part** si :

- moins de `absence_min_presents_pourcent` des élèves (50 % par défaut) ont pointé : jour férié,
  grève, coupure de courant au portail... ;
- une des pointeuses n'a pas répondu, ou aucune lecture n'a réussi depuis 5 minutes ;
- il est plus de 9 h + `age_max_minutes` (11 h par défaut) : PC allumé trop tard.

Dans ces cas, le journal (`logs/pointage.log`) l'indique une fois par jour.

## Qui paie : les parents ou l'école (forfait)

Section `[abonnement]` de `config.toml` :

- `paiement = "parents"` : chaque parent paie `prix_mensuel` après l'essai gratuit, saisi avec
  `pt paiement`. Sans paiement, ses messages s'arrêtent, et le relevé mensuel le liste à relancer.
- `paiement = "forfait"` : l'école paie pour tous ses élèves. Aucun paiement par élève, pas de
  relevé des parents : tous les parents reçoivent leurs messages, sans limite de date. Le suivi
  des tranches payées par l'école se fait en dehors du logiciel.

## Brancher WhatsApp (API Cloud de Meta)

Les messages partent directement chez Meta, sans intermédiaire : on ne paie que le tarif
WhatsApp (message « utilitaire », environ 2 à 3 FCFA au Sénégal, à vérifier sur la grille
officielle de Meta).

**Pour tester (numéro de test de Meta, gratuit) :**
1. Crée un portefeuille business sur https://business.facebook.com, puis un compte
   développeur sur https://developers.facebook.com.
2. « Créer une app » → cas d'usage « Se connecter avec les clients via WhatsApp ».
3. Page **WhatsApp > API Setup** : ajoute ton téléphone dans « To » (5 numéros au plus),
   puis recopie dans `.env` le jeton (`META_JETON`, valable 24 h) et le
   « Phone number ID » (`META_ID_NUMERO`).
4. `pt test-whatsapp TON_NUMERO --hello` : tu dois recevoir « Hello World ».
5. Pour une démo avec de vraies alertes **avant** l'approbation des modèles : écris
   « Bonjour » au numéro de test depuis le téléphone de démo, mets `texte_libre = true`
   et `mode = "meta"` dans `config.toml`. Pendant 24 h, ce téléphone reçoit les alertes
   en texte libre.

**Pour la production (de vrais parents) :**
WhatsApp interdit le texte libre à quelqu'un qui n'a pas écrit au numéro dans les
dernières 24 h. Il faut :
1. **Un numéro dédié** (une puce jamais utilisée sur l'application WhatsApp), ajouté dans
   WhatsApp Manager, avec le nom affiché « ARIA_point ».
2. **Un moyen de paiement** (carte Visa ou Mastercard) dans WhatsApp Manager.
3. **Quatre modèles de message**, catégorie « Utilitaire », langue « Français », à créer
   dans WhatsApp Manager > Modèles. Noms et textes (les noms doivent correspondre à
   `[notifications.modeles_meta]` dans `config.toml`) :

   | Nom | Texte |
   |---|---|
   | `aria_arrivee` | Bonjour, ici {{1}}. Votre enfant {{2}} est arrivé(e) à l'école à {{3}}. Message automatique ARIA_point. |
   | `aria_retard` | Bonjour, ici {{1}}. Votre enfant {{2}} est arrivé(e) en retard à l'école à {{3}}. Message automatique ARIA_point. |
   | `aria_sortie` | Bonjour, ici {{1}}. Votre enfant {{2}} a quitté l'école à {{3}}. Message automatique ARIA_point. |
   | `aria_absence` | Bonjour, ici {{1}}. Votre enfant {{2}} n'avait pas pointé à l'école à {{3}}. En cas d'erreur ou d'absence prévue, merci de contacter l'école. Message automatique ARIA_point. |

   Exemples à donner à Meta : {{1}} = `Groupe Scolaire Exemple`, {{2}} = `Awa Diop (6e A)`,
   {{3}} = `07h42`.
4. **Un jeton permanent** : business.facebook.com > Paramètres > Utilisateurs système >
   Ajouter (rôle Admin), lui donner l'app et le compte WhatsApp, puis « Générer un jeton »
   avec les autorisations `whatsapp_business_messaging` et `whatsapp_business_management`.
   À mettre dans `META_JETON`. Si un PC d'école est volé : même page, « Révoquer le jeton ».
5. **La vérification de l'entreprise** (RCCM, NINEA d'ARIA_tech) : sans elle, Meta limite
   l'envoi à 250 destinataires différents par 24 h, pour toutes les écoles réunies.
6. **Le consentement écrit des parents** à recevoir les alertes WhatsApp (exigé par WhatsApp).

À savoir : « envoyé » signifie « accepté par Meta ». La livraison et la lecture ne sont pas
suivies dans cette version (il faudrait un serveur joignable depuis internet).

## Lancer le service automatiquement au démarrage du PC

Planificateur de tâches Windows → Créer une tâche :
- Déclencheur : « À l'ouverture de session »
- Action : programme `C:\Users\DELL\Documents\pointage-scolaire\.venv\Scripts\python.exe`,
  arguments `main.py run`, démarrer dans `C:\Users\DELL\Documents\pointage-scolaire`
- Paramètres : « Si la tâche échoue, redémarrer toutes les 1 minute »

Le journal de fonctionnement est dans `logs/pointage.log`.

## Sauvegardes sur clé USB

Le service sauvegarde la base chaque jour dans `backups/`, **sur le même disque** que la base.
Si ce disque tombe en panne ou si le PC est volé, ces sauvegardes disparaissent avec lui.
D'où une copie quotidienne sur une clé USB (ou un disque externe) :

1. Sur la clé, crée un dossier `ARIA_point_sauvegardes` à la racine.
2. **Chiffre la clé** avec BitLocker To Go (clic droit sur la clé dans l'Explorateur >
   Activer BitLocker ; Windows Pro). Coche « Déverrouiller automatiquement sur ce PC »
   pour que le service puisse y écrire. Note le mot de passe de la clé dans un lieu sûr.
3. Dans `config.toml`, section `[sauvegarde]` : `copie_externe = "ARIA_point_sauvegardes"`.
4. Laisse la clé branchée. `pt sauvegarder` fait une copie tout de suite pour vérifier.

Chaque heure, si la dernière copie a plus de 24 h et que la clé est branchée, le service y
copie la dernière sauvegarde, la vérifie (une copie abîmée est supprimée), et garde
`retention_jours` jours d'historique. La lettre de la clé (E:, F:...) peut changer, le
logiciel cherche le dossier sur tous les lecteurs. Sans copie depuis 3 jours, un message
s'affiche sur le tableau de bord et dans `pt statut`.

**Restaurer après une panne** (nouveau PC, logiciel installé) : arrête le service, copie le
fichier `pointage_AAAA-MM-JJ_HHMMSS.db` le plus récent de la clé vers `data\pointage.db`
(supprime `data\pointage.db-wal` et `data\pointage.db-shm` s'ils existent), puis relance le
service. Les pointages encore dans la mémoire de la pointeuse depuis cette date sont
récupérés automatiquement à la lecture suivante, sauf si la mémoire a été vidée entre-temps
(les vidages gardent une copie brute dans `backups/memoire_pointeuse_*.json`).

## Mises à jour

Le logiciel tourne sur le PC de l'école : une correction faite chez ARIA_tech n'y arrive
pas toute seule. On lui envoie un **paquet** (`ARIA_point_1.1.0.zip`), qu'elle installe en
une commande. Rien n'est automatique, exprès : une mise à jour ratée un matin bloquerait
le portail au moment où les parents attendent les messages.

**Chez ARIA_tech (PC de développement)**

1. Augmente `__version__` dans `pointage/__init__.py` : `1.0.1` pour une correction,
   `1.1.0` pour une nouvelle fonction, `2.0.0` pour un gros changement.
2. Ajoute une section `## 1.1.0` en haut de `NOTES_DE_VERSION.md`, écrite pour le directeur.
3. Commite, puis `pt creer-paquet` : il refuse s'il reste des fichiers non commités ou si un
   test échoue, puis crée `dist/ARIA_point_1.1.0.zip`.
4. Envoie le zip à l'école (WhatsApp, email, clé USB).

**À l'école (le soir ou le week-end, jamais aux heures de passage)**

1. Arrête le service (Planificateur de tâches > clic droit sur la tâche > Terminer) et le
   tableau de bord.
2. Glisse le zip sur `mettre-a-jour.bat` (ou `pt mettre-a-jour ARIA_point_1.1.0.zip`), tape `MAJ`.
3. Relance le service et le tableau de bord.

Ce que fait la mise à jour, dans l'ordre :

- vérifie le paquet (complet, non modifié depuis sa création, version plus récente) ;
- refuse si le service ou le tableau de bord tourne, et les empêche de redémarrer pendant
  l'installation (un service relancé pendant ce temps s'arrête aussitôt, sans rien faire) ;
- met de côté la base et les fichiers qui vont changer dans `backups/mise_a_jour_.../` ;
- installe les fichiers, et les bibliothèques si `requirements.txt` a changé (internet
  nécessaire dans ce cas) ;
- lance le nouveau logiciel dans un programme séparé (`pt verifier-installation`) : il lit la
  configuration de l'école, met la structure de la base à jour et la contrôle ;
- **au moindre échec, remet tout comme avant** (fichiers et base) et dit pourquoi.

Une mise à jour ne touche jamais à `config.toml` (réglages de l'école), `.env`, `data/`,
`backups/`, `archives/`, `rapports/` ni `logs/`. Le `config.toml` livré est déposé à côté
sous le nom `config.reference.toml` : un réglage apparu dans la nouvelle version prend sa
valeur par défaut tant qu'il n'est pas ajouté au `config.toml` de l'école (la mise à jour
liste ces nouveaux réglages).

**Revenir en arrière plus tard** (un problème découvert en cours de journée) :
`pt annuler-mise-a-jour`. Si la structure de la base n'a pas changé, la base est gardée
telle quelle, aucun pointage n'est perdu. Sinon, l'ancienne version ne saurait pas la lire :
la base d'avant la mise à jour est remise (les pointages encore dans la mémoire de la
pointeuse sont relus, mais les paiements saisis depuis sont à ressaisir), et la base
actuelle est gardée à part.

`pt version` affiche la version du logiciel et celle de la base.

**Installer une nouvelle école** : dézippe le paquet dans un dossier (par exemple
`C:\ARIA_point`), puis suis « Installation » ci-dessus à partir de l'étape 1. Le fichier
`paquet.json` du zip indique la version installée.

**Pour le développeur : changer la structure de la base.** Ajouter une table : il suffit de
l'ajouter à `SCHEMA` dans `pointage/db.py`. Modifier une table existante (nouvelle colonne...) :
modifier `SCHEMA` **et** écrire une migration (`_migration_2`...), voir les explications dans
`db.py`. Ne jamais lancer `pt mettre-a-jour` dans le dossier de développement (il refuse).

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

Puis, à la rentrée : `pt rentree` (voir « Élèves qui partent, et rentrée scolaire »).

## Organisation du code

```
main.py                  ligne de commande
pointage/config.py       lecture de config.toml et .env
pointage/db.py           schéma SQLite et utilitaires de dates
pointage/device.py       vraie pointeuse (pyzk) et pointeuse simulée
pointage/collector.py    lecture, anti-doublon, classement arrivée/sortie/retard
pointage/eleves.py       fiches élèves, import CSV, numéros de téléphone
pointage/subscriptions.py essai gratuit, paiements, abonnement actif ou non
pointage/notifier.py     file d'attente et envoi WhatsApp (API Cloud de Meta, ou console)
pointage/backup.py       sauvegardes et rotation
pointage/purge.py        vidage sécurisé de la mémoire du boîtier
pointage/reports.py      statistiques mensuelles et PDF
pointage/archive.py      archivage de fin d'année
pointage/service.py      boucle du service continu
pointage/verrou.py       verrous : le service ou le tableau de bord tourne-t-il déjà ?
pointage/miseajour.py    paquets de mise à jour, installation, retour en arrière
pointage/web.py          tableau de bord web (Flask) ; pages dans pointage/templates/
tests/                   tests automatiques (pytest)
```

## Limites connues (V1)

- Un élève qui arrive très tard (après 16 h, `sortie_seule_apres`) sans avoir pointé le
  matin est compté comme une sortie : le logiciel suppose qu'il a oublié de pointer en arrivant.
- Un élève absent un jour sans aucun pointage dans toute l'école (grève, jour férié
  imprévu) n'est pas compté absent : ce jour n'est pas considéré comme un jour d'école.
- Paiements saisis à la main (pas encore de Wave ou d'Orange Money).

## Données personnelles

Les empreintes digitales restent **dans la pointeuse**. Le logiciel ne reçoit qu'un
numéro et une heure. La base contient cependant des données personnelles de mineurs
(noms, présences, téléphones des parents) : au Sénégal, leur traitement relève de la
loi n° 2008-12 et doit être déclaré à la CDP (Commission de protection des Données
Personnelles). Le consentement écrit des parents est à recueillir à l'inscription.
