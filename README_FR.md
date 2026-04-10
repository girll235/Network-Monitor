# Système de Surveillance Réseau Distribué (Network Monitoring System) 🚀

Ce projet est une application de surveillance des performances des machines sur un réseau, basée sur une architecture (Client/Serveur) et la programmation de sockets. Le système permet de surveiller la consommation du processeur (CPU) et de la mémoire (RAM) de plusieurs appareils simultanément grâce à une interface graphique (GUI) avancée.

## 📌 Caractéristiques du projet
- **Prise en charge de protocoles doubles :** Fonctionne via les protocoles **TCP** (pour une connexion fiable) et **UDP** (pour la rapidité et la comparaison).
- **Interface graphique (GUI) :** Interfaces interactives pour le serveur et le client utilisant la bibliothèque `Tkinter`.
- **Multi-threading :** Capacité à gérer un grand nombre de clients (Agents) en même temps.
- **Analyse des performances :** Calcul des moyennes de consommation et suivi de l'état des appareils (actif/inactif) en temps réel.
- **Simulation d'attaques (Attack Simulation) :** Fonctionnalité intégrée pour tester la capacité du serveur à supporter une charge élevée de messages (Flood).
- **Gestion intelligente des erreurs :** Détection automatique des messages mal formés ou des déconnexions soudaines.

## 🏗️ Architecture du système
Le projet suit le modèle **Client-Serveur** :
- **Agent (Client) :** Collecte les métriques locales du système et les envoie selon un format de protocole spécifique.
- **Collector (Serveur) :** Reçoit les données, les traite, les affiche dans des tableaux et surveille l'état d'activité de chaque client.

## 📝 Spécification du protocole
La communication s'effectue via les messages suivants :
- `HELLO <agent_id> <hostname>` : Pour enregistrer le client auprès du serveur.
- `REPORT <agent_id> <timestamp> <cpu> <ram>` : Pour envoyer les rapports de performance périodiques.
- `BYE <agent_id>` : Pour fermer la connexion proprement.
- Le serveur répond par `OK` en cas de succès ou `ERROR` en cas de problème de formatage.

## 🛠️ Prérequis techniques
- **Langage :** Python 3.x
- **Bibliothèques standards :** `socket`, `threading`, `time`, `tkinter`, `json`.
- **Bibliothèques externes (à installer) :**
  ```bash
  pip install psutil
  ```

---

## 🚀 Manuel d'utilisation (Instructions importantes)

### 1. Démarrage du Serveur (Server)
Lancez d'abord le fichier du serveur pour ouvrir les canaux de réception :
```bash
python server.py
```
Le serveur ouvrira une interface graphique affichant l'état des clients et les rapports reçus via les ports TCP et UDP.

### 2. Démarrage du Client (Agent)
Vous pouvez lancer le client via la commande suivante :
```bash
python client.py
```

**⚠️ Alertes importantes avant de cliquer sur le bouton Start :**
1.  **Changement de nom de l'Agent (Agent ID) :** L'application permet à l'utilisateur de modifier l'identifiant de l'appareil (Agent ID) directement via l'interface des paramètres **avant** de lancer la connexion. Cela permet de personnaliser l'affichage de l'appareil et de l'identifier facilement sur l'écran central du serveur.
2.  **Vérification de l'IP du serveur :** Il est impératif de vérifier que l'adresse **IP** du serveur est correctement saisie dans le champ dédié avant de cliquer sur le bouton **Start**. Sans une configuration correcte de l'adresse IP, le client ne pourra pas établir de connexion avec le serveur.

---

## 🧪 Cas de tests (Test Cases)
Le système a été testé avec succès dans les cas suivants :
- **Connexion d'un seul client :** Vérification de l'envoi et de la réception des messages HELLO et REPORT.
- **Plusieurs clients simultanés :** Fonctionnement de plusieurs agents en même temps et capacité du serveur à séparer leurs données.
- **Détection d'inactivité :** Le serveur cesse de considérer un client comme "actif" s'il n'envoie pas de rapport pendant une durée supérieure à $3 \times T$ secondes (15 secondes).
- **Messages mal formés :** Envoi de données incorrectes pour vérifier que le serveur répond par `ERROR`.
- **Simulation de charge :** Test du protocole UDP en cas d'envoi intensif pour comparer la perte de paquets.

## 👥 Équipe de projet
* [SABRINE BEN TILI](https://www.linkedin.com/in/sabrine-ben-tili/) [@girll235](https://github.com/girll235/)
* [MOHAMED AMMOUS](https://www.facebook.com/mouhamed.ammous) [@MohamedAmmous](https://github.com/MohamedAmmous)
