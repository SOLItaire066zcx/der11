import logging
import random
import json
import csv
import os
import datetime
import sqlite3
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters, ConversationHandler
)
import secrets
from telegram.constants import ParseMode
import io
import shutil
import sys
import glob

# Token du bot
TOKEN = "8057509848:AAHJsE1q63yn9OgBFftKiE8MUqOpidilBuw"

# Constantes pour les choix
POSITIONS = ["1", "2", "3", "4", "5"]
COTES = ["1.23", "1.54"]
SIDES = ["Gauche", "Droite"]

# Configuration des chemins
IMAGES_DIR = "images/cases"
IMAGE_EXT = "jpg"

# Fichier de base de données SQLite
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_FILE = os.path.join(SCRIPT_DIR, "apple_predictor.db")

# États de conversation
ASK_RESULTS, ASK_CASES, ASK_SIDE, ASK_BONNE_MAUVAISE, ASK_1XBET_ID, RESET_CONFIRM, ASK_BET_AMOUNT, ASK_EXPORT_FORMAT = range(8)

# Cache mémoire pour les informations utilisateur temporaires
user_memory = {}

# Ajout de l'ID Telegram de l'admin (à personnaliser)
ADMIN_TELEGRAM_ID = 7569017578  # ID Telegram de l'administrateur

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Messages du bot
MESSAGES = {
    "welcome": "🍏 Bienvenue sur Apple Predictor Bot !\nCe bot simule le fonctionnement du jeu Apple of Fortune sur 1xbet : à chaque niveau, une case gagnante aléatoire (aucune astuce possible).\nNouveau : Précision sur le comptage des cases : pour chaque prédiction, tu sauras s'il faut compter depuis la gauche ou la droite !\nTu peux suivre tes statistiques, enregistrer tes parties, profiter de conseils pour jouer responsable, et importer/exporter ton historique.\n\nMenu ci-dessous 👇",
    "error": "❌ Une erreur s'est produite. Veuillez réessayer.",
    "no_image": "⚠️ Image non disponible pour cette direction."
}

# Limites globales par défaut
MAX_PREDICTIONS_PER_DAY = 10
MAX_PREDICTIONS_PER_HOUR = 3
MAX_PREDICTIONS_TOTAL = 100

# Initialisation de la base de données
def init_db():
    """Initialise la base de données SQLite en créant les tables si elles n'existent pas."""
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # Table des utilisateurs
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT,
            username TEXT
        );
        ''')

        # Table de l'historique
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            type TEXT,
            cote TEXT,
            case_number TEXT,
            side TEXT,
            side_ref TEXT,
            resultat TEXT,
            date TEXT,
            heure TEXT,
            seconde TEXT,
            bet_amount TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        ''')


# Table des codes d'accès (ajout de for_user_id et used)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS access_codes (
            code TEXT PRIMARY KEY,
            for_user_id TEXT,
            expiration DATETIME,
            used INTEGER DEFAULT 0
        );
        ''')
        # Table des accès utilisateurs
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_access (
            user_id TEXT PRIMARY KEY,
            expiration DATETIME
        );
        ''')
        # Ajout de la colonne suspended si elle n'existe pas
        cursor.execute("PRAGMA table_info(user_access)")
        columns = [row[1] for row in cursor.fetchall()]
        if "suspended" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN suspended INTEGER DEFAULT 0")
        # Ajout des colonnes pour les limites et compteurs si elles n'existent pas
        if "predictions_today" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN predictions_today INTEGER DEFAULT 0")
        if "last_prediction_day" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN last_prediction_day TEXT")
        if "predictions_hour" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN predictions_hour INTEGER DEFAULT 0")
        if "last_prediction_hour" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN last_prediction_hour TEXT")
        if "predictions_total" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN predictions_total INTEGER DEFAULT 0")
        if "limit_per_day" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN limit_per_day INTEGER")
        if "limit_per_hour" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN limit_per_hour INTEGER")
        if "limit_total" not in columns:
            cursor.execute("ALTER TABLE user_access ADD COLUMN limit_total INTEGER")
        conn.commit()
        logging.info("Base de données initialisée avec succès.")
    except sqlite3.Error as e:
        logging.error(f"Erreur lors de l'initialisation de la base de données : {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

# Fonctions utilitaires
def get_rng(user_id_1xbet=None, bet_amount_for_rng=None):
    """Obtient un générateur de nombres aléatoires, éventuellement initialisé avec un seed."""
    if user_id_1xbet or bet_amount_for_rng:
        now = datetime.datetime.now()
        now_str = now.strftime("%Y%m%d_%H%M%S_%f")
        seed = f"{user_id_1xbet}_{now_str}_{bet_amount_for_rng}"
        seed_parts = [part for part in seed.split('_') if part != 'None']
        seed = '_'.join(seed_parts)
        return random.Random(seed), seed
    else:
        return random.SystemRandom(), None

def get_user_history(user_id):
    """Récupère l'historique d'un utilisateur depuis la base de données."""
    conn = None
    history = []
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT type, cote, case_number, side, side_ref, resultat, date, heure, seconde, bet_amount 
            FROM history 
            WHERE user_id = ? 
            ORDER BY history_id
        """, (user_id,))
        rows = cursor.fetchall()
        for row in rows:
            history.append({
                "type": row[0],
                "cote": row[1],
                "case": row[2],
                "side": row[3],
                "side_ref": row[4],
                "resultat": row[5],
                "date": row[6],
                "heure": row[7],
                "seconde": row[8],
                "bet_amount": row[9]
            })
    except sqlite3.Error as e:
        logging.error(f"Erreur lors de la récupération de l'historique pour l'utilisateur {user_id}: {e}")
    finally:
        if conn:
            conn.close()
    return history


def contains_scam_words(txt):
    """Vérifie si le texte contient des mots suspects."""
    mots_suspects = [
        "hack", "triche", "cheat", "bot miracle", "code promo", "astuce", "secret", 
        "gagner sûr", "prédiction sûre", "script", "seed", "crack", "pirater", "mod", 
        "prédire sûr", "bug", "exploit", "tricher", "logiciel"
    ]
    return any(mot in txt.lower() for mot in mots_suspects)

def current_time_data():
    """Retourne un dictionnaire avec la date et l'heure actuelles."""
    now = datetime.datetime.now()
    return {
        "date": now.strftime("%d/%m"),
        "heure": now.strftime("%H:%M"),
        "seconde": now.strftime("%S")
    }

def get_main_menu():
    """Retourne le menu principal du bot."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🍏 Prédire"), KeyboardButton("ℹ️ Fonctionnement")],
            [KeyboardButton("🎯 Conseils"), KeyboardButton("🚨 Arnaques")],
            [KeyboardButton("❓ FAQ"), KeyboardButton("📞 Contact")],
            [KeyboardButton("📝 Tutoriel"), KeyboardButton("ℹ️ À propos")],
            [KeyboardButton("🧠 Historique"), KeyboardButton("📊 Statistiques")],
            [KeyboardButton("📤 Exporter"), KeyboardButton("📥 Importer")],
            [KeyboardButton("♻️ Réinitialiser historique"), KeyboardButton("🔄 Réinitialiser choix")],
            [KeyboardButton("🆘 Aide")]
        ],
        resize_keyboard=True
    )

def create_case_image(case_number, direction):
    """Crée une image pour une case spécifique avec sa direction."""
    # Créer une image de 400x400 pixels avec fond blanc
    img = Image.new('RGB', (400, 400), 'white')
    draw = ImageDraw.Draw(img)
    
    # Dessiner un cercle pour la case
    circle_color = (255, 165, 0)  # Orange
    draw.ellipse([100, 100, 300, 300], fill=circle_color, outline='black', width=3)
    
    # Ajouter le numéro de la case
    try:
        font = ImageFont.truetype("arial.ttf", 100)
    except:
        font = ImageFont.load_default()
    
    text = str(case_number)
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    
    x = (400 - text_width) // 2
    y = (400 - text_height) // 2
    draw.text((x, y), text, fill='black', font=font)
    
    # Ajouter la flèche de direction
    if direction == "gauche":
        # Flèche vers la gauche
        draw.polygon([(50, 200), (150, 150), (150, 250)], fill='black')
    else:
        # Flèche vers la droite
        draw.polygon([(350, 200), (250, 150), (250, 250)], fill='black')
    
    # Créer le dossier s'il n'existe pas
    os.makedirs(IMAGES_DIR, exist_ok=True)
    
    # Sauvegarder l'image
    filename = os.path.join(IMAGES_DIR, f"case{case_number}_{direction}.{IMAGE_EXT}")
    img.save(filename, "JPEG")
    return filename

def ensure_case_images():
    """S'assure que toutes les images nécessaires existent."""
    for case in range(1, 6):
        for direction in ["gauche", "droite"]:
            image_path = os.path.join(IMAGES_DIR, f"case{case}_{direction}.{IMAGE_EXT}")
            if not os.path.exists(image_path):
                create_case_image(case, direction)
                logger.info(f"Image créée : {image_path}")

# Fonctions de base du bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fonction de démarrage du bot."""
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text(
            "⛔️ Vous n'avez pas accès à ce bot sans code d'accès.\n"
            "Veuillez contacter l'administrateur pour en obtenir un :\n"
            "• WhatsApp : https://wa.me/+2250501945735\n"
            "• Téléphone 1 : 0500448208\n"
            "• Téléphone 2 : 0501945735\n"
            "• Telegram : @Roidesombres225"
        )
        return
    first_name = update.effective_user.first_name or ""
    last_name = update.effective_user.last_name or ""
    username = update.effective_user.username or ""
    full_name = f"{first_name} {last_name}".strip()

    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        user_exists = cursor.fetchone()

        if not user_exists:
            cursor.execute("INSERT INTO users (user_id, name, username) VALUES (?, ?, ?)",
                         (user_id, full_name, username))
            conn.commit()
            logging.info(f"Nouvel utilisateur ajouté : {user_id}")
        else:
            cursor.execute("UPDATE users SET name = ?, username = ? WHERE user_id = ?",
                         (full_name, username, user_id))
            conn.commit()
            logging.info(f"Informations utilisateur mises à jour : {user_id}")

    except sqlite3.Error as e:
        logging.error(f"Erreur base de données dans start pour l'utilisateur {user_id}: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    await update.message.reply_text(
        MESSAGES["welcome"],
        reply_markup=get_main_menu()
    )

async def fonctionnement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    """Explique le fonctionnement du bot."""
    msg = (
        "🍏 Fonctionnement Apple of Fortune (1xbet, cotes 1.23 et 1.54) 🍏\n\n"
        "Le jeu utilise un algorithme appelé RNG (Random Number Generator), qui choisit la case gagnante totalement au hasard à chaque niveau. "
        "Il est donc impossible de prédire ou d'influencer le résultat, chaque case a 20% de chance d'être gagnante.\n\n"
        "Notre bot applique le même principe : pour chaque prédiction, la case est tirée au sort grâce à un RNG sécurisé, exactement comme sur 1xbet. "
        "Si tu veux, tu peux fournir ton ID utilisateur 1xbet pour obtenir une simulation personnalisée (la même suite de cases pour ce seed, basé sur ton ID, la date et l'heure)."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def conseils(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    """Affiche les conseils de jeu responsable."""
    msg = (
        "🎯 Conseils de jeu responsable sur 1xbet :\n\n"
        "- Fixe-toi une limite de pertes.\n"
        "- Ne mise jamais l'argent que tu ne peux pas perdre.\n"
        "- Le jeu est 100% hasard, chaque case a autant de chances d'être gagnante.\n"
        "- Prends du recul après une série de jeux."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def arnaques(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    """Affiche les avertissements sur les arnaques."""
    msg = (
        "🚨 Attention aux arnaques sur 1xbet !\n\n"
        "Aucune application, bot, code promo ou script ne peut prédire la bonne case.\n"
        "Ceux qui promettent le contraire veulent te tromper ou te faire perdre de l'argent.\n"
        "Ne partage jamais tes identifiants 1xbet."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    """Affiche les informations de contact."""
    msg = (
        "📞 Contact & Aide :\n"
        "• WhatsApp : [wa.me/+2250501945735](https://wa.me/+2250501945735)\n"
        "• Téléphone 1 : 0500448208\n"
        "• Téléphone 2 : 0501945735\n"
        "• Telegram : [@Roidesombres225](https://t.me/Roidesombres225)\n"
        "N'hésite pas à me contacter pour toute question ou aide !"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    """Affiche la FAQ."""
    msg = (
        "❓ FAQ Apple of Fortune (1xbet, cotes 1.23 et 1.54)\n\n"
        "- Peut-on prédire la bonne case ? Non, c'est impossible, chaque case a 20% de chance.\n"
        "- Un code promo change-t-il le hasard ? Non.\n"
        "- Le bot donne des suggestions purement aléatoires, comme sur 1xbet.\n"
        "- Le bot précise maintenant le sens de comptage des cases pour éviter toute erreur."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def tuto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    """Affiche le tutoriel."""
    text = (
        "📝 Tutoriel rapide\n\n"
        "- Clique sur 🍏 Prédire pour obtenir les cases suggérées (1.23 puis 1.54).\n"
        "- Le bot t'indique non seulement la case, mais aussi s'il faut compter depuis la gauche ou la droite.\n"
        "- Joue ces cases sur le site 1xbet. Indique si tu as joué à gauche ou à droite de la case, puis si tu as eu 'Bonne' ou 'Mauvaise' pour chaque cote.\n"
        "- Consulte ton historique et tes statistiques pour progresser.\n"
        "- Tu peux aussi exporter/importer ton historique via le menu."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def apropos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    """Affiche les informations à propos du bot."""
    text = (
        "ℹ️ À propos\n"
        "Bot éducatif créé par SOLITAIRE HACK, adapté pour 1xbet (cotes 1.23 et 1.54 uniquement, précision sur le sens de comptage des cases)."
    )
    await update.message.reply_text(text, parse_mode="Markdown")
async def stats_perso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les statistiques personnelles de l'utilisateur."""
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM history WHERE user_id = ?", (user_id,))
        total_entries = cursor.fetchone()[0]
        total_sequences = total_entries // 2

        if total_sequences == 0:
            await update.message.reply_text(
                "Aucune statistique disponible pour l'instant, joue une séquence pour commencer.",
                reply_markup=get_main_menu()
            )
            return

        cursor.execute("""
            SELECT cote, resultat, COUNT(*) 
            FROM history 
            WHERE user_id = ? AND (resultat = 'Bonne' OR resultat = 'Mauvaise') 
            GROUP BY cote, resultat
        """, (user_id,))
        results = cursor.fetchall()

        victoire_123 = victoire_154 = defaites_123 = defaites_154 = 0

        for cote, resultat, count in results:
            if cote == "1.23":
                if resultat == "Bonne":
                    victoire_123 = count
                elif resultat == "Mauvaise":
                    defaites_123 = count
            elif cote == "1.54":
                if resultat == "Bonne":
                    victoire_154 = count
                elif resultat == "Mauvaise":
                    defaites_154 = count

        taux_123 = round((victoire_123 / (victoire_123 + defaites_123)) * 100, 1) if (victoire_123 + defaites_123) > 0 else 0
        taux_154 = round((victoire_154 / (victoire_154 + defaites_154)) * 100, 1) if (victoire_154 + defaites_154) > 0 else 0

        txt = (
            f"📊 Tes statistiques\n"
            f"- Séquences jouées : {total_sequences}\n"
            f"- Victoires cote 1.23 : {victoire_123} | Défaites : {defaites_123} | Taux : {taux_123}%\n"
            f"- Victoires cote 1.54 : {victoire_154} | Défaites : {defaites_154} | Taux : {taux_154}%\n"
        )
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=get_main_menu())

    except sqlite3.Error as e:
        logging.error(f"Erreur base de données pour les stats de l'utilisateur {user_id}: {e}")
        await update.message.reply_text(
            MESSAGES["error"],
            reply_markup=get_main_menu()
        )
    finally:
        if conn:
            conn.close()

async def historique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche l'historique des parties de l'utilisateur."""
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    memory = get_user_history(user_id)
    
    if not memory:
        await update.message.reply_text(
            "Aucun historique enregistré pour l'instant.",
            reply_markup=get_main_menu()
        )
        return

    sequences = []
    for i in range(0, len(memory), 2):
        try:
            a = memory[i]
            b = memory[i+1]
        except IndexError:
            continue

        date = a.get("date", "-")
        heure = a.get("heure", "-")
        sec = a.get("seconde", "-")
        bet_amount = a.get("bet_amount", "-")
        case123 = a.get("case", "?")
        sens123 = a.get("side", "?")
        res123 = a.get("resultat", "?")
        case154 = b.get("case", "?")
        sens154 = b.get("side", "?")
        res154 = b.get("resultat", "?")
        etat = "🏆" if a.get("type") == "gagne" else "💥"
        
        seq = (
            f"📅 {date} à {heure}:{sec} | Mise : {bet_amount}\n"
            f"1️⃣ Cote 1.23 : Case {case123} ({sens123}) — {res123}\n"
            f"2️⃣ Cote 1.54 : Case {case154} ({sens154}) — {res154}\n"
            f"Résultat : {etat}\n"
            f"--------------------"
        )
        sequences.append(seq)

    msg = "🧠 Historique de tes 15 dernières séquences :\n\n" + "\n".join(sequences[-15:])
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [
                [KeyboardButton("♻️ Réinitialiser historique")],
                [KeyboardButton("⬅️ Menu principal")]
            ],
            resize_keyboard=True
        )
    )

async def reset_historique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demande confirmation pour réinitialiser l'historique."""
    await update.message.reply_text(
        "⚠️ Veux-tu vraiment supprimer tout ton historique ?\nRéponds OUI pour confirmer, NON pour annuler.",
        reply_markup=ReplyKeyboardMarkup([["OUI", "NON"]], resize_keyboard=True)
    )
    context.user_data["awaiting_reset"] = True
    return RESET_CONFIRM

async def handle_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la confirmation de réinitialisation de l'historique."""
    if context.user_data.get("awaiting_reset"):
        if update.message.text.strip().upper() == "OUI":
            user_id = str(update.effective_user.id)
            conn = None
            try:
                conn = sqlite3.connect(DATABASE_FILE)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
                conn.commit()
                logging.info(f"Historique réinitialisé pour l'utilisateur {user_id}")
                context.user_data["awaiting_reset"] = False
                await update.message.reply_text("✅ Ton historique a été réinitialisé.", reply_markup=get_main_menu())
                return ConversationHandler.END
            except sqlite3.Error as e:
                logging.error(f"Erreur base de données lors de la réinitialisation pour l'utilisateur {user_id}: {e}")
                if conn:
                    conn.rollback()
                context.user_data["awaiting_reset"] = False
                await update.message.reply_text(
                    MESSAGES["error"],
                    reply_markup=get_main_menu()
                )
                return ConversationHandler.END
            finally:
                if conn:
                    conn.close()
        else:
            context.user_data["awaiting_reset"] = False
            await update.message.reply_text("❌ Réinitialisation annulée.", reply_markup=get_main_menu())
            return ConversationHandler.END
    return ConversationHandler.END

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exporte l'historique au format CSV."""
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    memory = get_user_history(user_id)
    
    if not memory:
        await update.message.reply_text("Aucun historique à exporter.", reply_markup=get_main_menu())
        return ConversationHandler.END if 'export_format_choice' in context.user_data else None


    rows = []
    user_info = {}
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT name, username FROM users WHERE user_id = ?", (user_id,))
        user_row = cursor.fetchone()
        if user_row:
            user_info["name"] = user_row[0] or ""
            user_info["username"] = user_row[1] or ""
    except sqlite3.Error as e:
        logging.error(f"Erreur base de données pour l'export CSV de l'utilisateur {user_id}: {e}")
    finally:
        if conn:
            conn.close()

    for entry in memory:
        rows.append({
            "user_id": user_id,
            "name": user_info.get("name", ""),
            "username": user_info.get("username", ""),
            "type": entry.get("type", ""),
            "cote": entry.get("cote", ""),
            "case": entry.get("case", ""),
            "side": entry.get("side", ""),
            "side_ref": entry.get("side_ref", ""),
            "resultat": entry.get("resultat", ""),
            "date": entry.get("date", ""),
            "heure": entry.get("heure", ""),
            "seconde": entry.get("seconde", ""),
            "bet_amount": entry.get("bet_amount", "")
        })

    csv_filename = f"history_export_{user_id}.csv"
    try:
        with open(csv_filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["user_id", "name", "username", "type", "cote", "case", "side", "side_ref", "resultat", "date", "heure", "seconde", "bet_amount"])
            writer.writeheader()
            writer.writerows(rows)

        await update.message.reply_document(document=open(csv_filename, "rb"), filename=csv_filename)
        await update.message.reply_text("✅ Exportation CSV terminée !", reply_markup=get_main_menu())
    except Exception as e:
        logging.error(f"Erreur lors de l'export CSV pour l'utilisateur {user_id}: {e}")
        await update.message.reply_text(MESSAGES["error"], reply_markup=get_main_menu())
    finally:
        try:
            if os.path.exists(csv_filename):
                os.remove(csv_filename)
        except OSError as e:
            logging.error(f"Erreur lors de la suppression du fichier {csv_filename}: {e}")
    return ConversationHandler.END

async def export_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exporte l'historique au format TXT."""
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    memory = get_user_history(user_id)
    
    if not memory:
        await update.message.reply_text("Aucun historique à exporter.", reply_markup=get_main_menu())
        return ConversationHandler.END if 'export_format_choice' in context.user_data else None

    sequences = []
    for i in range(0, len(memory), 2):
        try:
            a = memory[i]
            b = memory[i+1]
        except IndexError:
            continue

        date = a.get("date", "-")
        heure = a.get("heure", "-")
        sec = a.get("seconde", "-")
        bet_amount = a.get("bet_amount", "-")
        case123 = a.get("case", "?")
        sens123 = a.get("side", "?")
        res123 = a.get("resultat", "?")
        case154 = b.get("case", "?")
        sens154 = b.get("side", "?")
        res154 = b.get("resultat", "?")
        etat = "🏆" if a.get("type") == "gagne" else "💥"
        
        seq = (
            f"📅 {date} à {heure}:{sec} | Mise : {bet_amount}\n"
            f"1️⃣ Cote 1.23 : Case {case123} ({sens123}) — {res123}\n"
            f"2️⃣ Cote 1.54 : Case {case154} ({sens154}) — {res154}\n"
            f"Résultat : {etat}\n"
            f"--------------------"
        )
        sequences.append(seq)

    txt_content = "\n".join(sequences[-100:])
    txt_filename = f"history_export_{user_id}.txt"
    try:
        with open(txt_filename, "w", encoding="utf-8") as f:
            f.write(txt_content)

        await update.message.reply_document(document=open(txt_filename, "rb"), filename=txt_filename)
        await update.message.reply_text("✅ Exportation TXT terminée !", reply_markup=get_main_menu())
    except Exception as e:
        logging.error(f"Erreur lors de l'export TXT pour l'utilisateur {user_id}: {e}")
        await update.message.reply_text(MESSAGES["error"], reply_markup=get_main_menu())
    finally:
        try:
            if os.path.exists(txt_filename):
                os.remove(txt_filename)
        except OSError as e:
            logging.error(f"Erreur lors de la suppression du fichier {txt_filename}: {e}")
    return ConversationHandler.END

async def export_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exporte l'historique au format JSON."""
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    memory = get_user_history(user_id)

    if not memory:
        await update.message.reply_text("Aucun historique à exporter.", reply_markup=get_main_menu())
        return ConversationHandler.END if 'export_format_choice' in context.user_data else None

    user_info = {}
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT name, username FROM users WHERE user_id = ?", (user_id,))
        user_row = cursor.fetchone()
        if user_row:
            user_info["name"] = user_row[0] or ""
            user_info["username"] = user_row[1] or ""
    except sqlite3.Error as e:
        logging.error(f"Erreur base de données pour l'export JSON de l'utilisateur {user_id}: {e}")
    finally:
        if conn:
            conn.close()

    user_history_data = {
        user_id: {
            "name": user_info.get("name", ""),
            "username": user_info.get("username", ""),
            "history": memory
        }
    }

    json_filename = f"history_export_{user_id}.json"
    try:
        with open(json_filename, "w", encoding="utf-8") as f:
            json.dump(user_history_data, f, ensure_ascii=False, indent=2)

        await update.message.reply_document(document=open(json_filename, "rb"), filename=json_filename)
        await update.message.reply_text("✅ Exportation JSON terminée !", reply_markup=get_main_menu())
    except Exception as e:
        logging.error(f"Erreur lors de l'export JSON pour l'utilisateur {user_id}: {e}")
        await update.message.reply_text(MESSAGES["error"], reply_markup=get_main_menu())
    finally:
        try:
            if os.path.exists(json_filename):
                os.remove(json_filename)
        except OSError as e:
            logging.error(f"Erreur lors de la suppression du fichier {json_filename}: {e}")
    return ConversationHandler.END

async def ask_export_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demande le format d'exportation souhaité."""
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return ConversationHandler.END
    memory = get_user_history(user_id)
    if not memory:
        await update.message.reply_text("Aucun historique à exporter.", reply_markup=get_main_menu())
        return ConversationHandler.END

    await update.message.reply_text(
        "Quel format souhaites-tu pour l'exportation ?",
        reply_markup=ReplyKeyboardMarkup([["JSON", "CSV", "TXT"], ["⬅️ Menu principal"]], resize_keyboard=True)
    )
    return ASK_EXPORT_FORMAT

async def handle_export_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le choix du format d'exportation."""
    choice = update.message.text.strip().upper()
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return ConversationHandler.END
    memory = get_user_history(user_id)
    if not memory and choice != "⬅️ MENU PRINCIPAL":
        await update.message.reply_text("Aucun historique à exporter.", reply_markup=get_main_menu())
        return ConversationHandler.END

    if choice == "JSON":
        return await export_json(update, context)
    elif choice == "CSV":
        return await export_csv(update, context)
    elif choice == "TXT":
        return await export_txt(update, context)
    elif choice == "⬅️ MENU PRINCIPAL":
        await update.message.reply_text("Opération annulée.", reply_markup=get_main_menu())
        context.user_data.pop('export_format_choice', None)
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "Format inconnu. Choisis entre JSON, CSV ou TXT.",
            reply_markup=ReplyKeyboardMarkup([["JSON", "CSV", "TXT"], ["⬅️ Menu principal"]], resize_keyboard=True)
        )
        return ASK_EXPORT_FORMAT

async def import_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'importation des données."""
    if update.message.document:
        file = await update.message.document.get_file()
        filename = update.message.document.file_name
        user_id = str(update.effective_user.id)
        if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
            await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
            return

        imported_data = None
        import_successful = False

        if filename.endswith(".json"):
            try:
                content = await file.download_as_bytearray()
                data = json.loads(content.decode("utf-8"))
                if data and isinstance(data, dict):
                    imported_user_ids = list(data.keys())
                    if imported_user_ids:
                        first_imported_user_id = imported_user_ids[0]
                        imported_user_data = data[first_imported_user_id]
                        if isinstance(imported_user_data, dict) and "history" in imported_user_data and isinstance(imported_user_data["history"], list):
                            imported_data = {
                                user_id: {
                                    "name": imported_user_data.get("name", ""),
                                    "username": imported_user_data.get("username", ""),
                                    "history": imported_user_data["history"]
                                }
                            }
                            import_successful = True
                            await update.message.reply_text(
                                "⚠️ Tu es sur le point d'importer des données JSON. "
                                "Ceci remplacera TOUT ton historique actuel.\n"
                                "Réponds OUI pour confirmer, NON pour annuler.",
                                reply_markup=ReplyKeyboardMarkup([["OUI", "NON"]], resize_keyboard=True)
                            )
                        else:
                            await update.message.reply_text("Le format du fichier JSON semble incorrect.", reply_markup=get_main_menu())
                    else:
                        await update.message.reply_text("Aucune donnée utilisateur trouvée dans le fichier JSON.", reply_markup=get_main_menu())
                else:
                    await update.message.reply_text("Le format du fichier JSON semble incorrect.", reply_markup=get_main_menu())
            except Exception as e:
                logging.error(f"Erreur lors de l'import JSON pour l'utilisateur {user_id}: {e}")
                await update.message.reply_text(f"Erreur lors de l'import JSON : {e}", reply_markup=get_main_menu())

        elif filename.endswith(".csv"):
            try:
                content = await file.download_as_bytearray()
                import io
                reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
                required_csv_fields = ["user_id", "type", "cote", "case", "side", "side_ref", "resultat", "date", "heure", "seconde", "bet_amount"]
                if not all(field in reader.fieldnames for field in required_csv_fields):
                    await update.message.reply_text(f"Le fichier CSV doit contenir les colonnes suivantes : {', '.join(required_csv_fields)}.", reply_markup=get_main_menu())
                    return

                imported_history = []
                imported_user_info = {"name": "", "username": ""}
                first_row_processed = False

                for row in reader:
                    if not first_row_processed:
                        imported_user_info["name"] = row.get("name", "")
                        imported_user_info["username"] = row.get("username", "")
                        first_row_processed = True

                    imported_history.append({
                        "type": row.get("type", ""),
                        "cote": row.get("cote", ""),
                        "case": row.get("case", ""),
                        "side": row.get("side", ""),
                        "side_ref": row.get("side_ref", ""),
                        "resultat": row.get("resultat", ""),
                        "date": row.get("date", ""),
                        "heure": row.get("heure", ""),
                        "seconde": row.get("seconde", ""),
                        "bet_amount": row.get("bet_amount", "")
                    })

                if imported_history:
                    imported_data = {
                        user_id: {
                            "name": imported_user_info["name"],
                            "username": imported_user_info["username"],
                            "history": imported_history
                        }
                    }
                    import_successful = True
                    await update.message.reply_text(
                        "⚠️ Tu es sur le point d'importer des données CSV. "
                        "Ceci remplacera TOUT ton historique actuel.\n"
                        "Réponds OUI pour confirmer, NON pour annuler.",
                        reply_markup=ReplyKeyboardMarkup([["OUI", "NON"]], resize_keyboard=True)
                    )
                else:
                    await update.message.reply_text("Aucune donnée valide trouvée dans le fichier CSV.", reply_markup=get_main_menu())

            except Exception as e:
                logging.error(f"Erreur lors de l'import CSV pour l'utilisateur {user_id}: {e}")
                await update.message.reply_text(f"Erreur lors de l'import CSV : {e}", reply_markup=get_main_menu())

        elif filename.endswith(".txt"):
            try:
                content = await file.download_as_bytearray()
                text_content = content.decode("utf-8")
                sequences_text = text_content.split("--------------------")

                imported_history = []
                import re
                date_time_m = re.compile(r"📅 (.*) à (.*):(.*) \| Mise : (.*)")
                cote_m = re.compile(r"[12]️⃣ Cote (.*) : Case (.*) \((.*)\) — (.*)")
                result_m = re.compile(r"Résultat : (.*)")


                for seq_text in sequences_text:
                    lines = seq_text.strip().split('\n')
                    if len(lines) >= 4:
                        try:
                            date_heure_sec_mise = date_time_m.match(lines[0])
                            cote123_details = cote_m.match(lines[1])
                            cote154_details = cote_m.match(lines[2])
                            overall_result = result_m.match(lines[3])

                            if date_heure_sec_mise and cote123_details and cote154_details and overall_result:
                                date, heure, seconde, bet_amount = date_heure_sec_mise.groups()
                                result_type = "gagne" if "🏆" in overall_result.group(1) else "perdu"

                                cote123, case123, sens123, res123 = cote123_details.groups()
                                imported_history.append({
                                    "type": result_type,
                                    "cote": cote123,
                                    "case": case123,
                                    "side": sens123,
                                    "side_ref": "?",
                                    "resultat": res123,
                                    "date": date,
                                    "heure": heure,
                                    "seconde": seconde,
                                    "bet_amount": bet_amount
                                })

                                cote154, case154, sens154, res154 = cote154_details.groups()
                                imported_history.append({
                                    "type": result_type,
                                    "cote": cote154,
                                    "case": case154,
                                    "side": sens154,
                                    "side_ref": "?",
                                    "resultat": res154,
                                    "date": date,
                                    "heure": heure,
                                    "seconde": seconde,
                                    "bet_amount": bet_amount
                                })

                        except Exception as parse_error:
                            logging.warning(f"Impossible de parser la séquence dans l'import TXT : {lines[0] if lines else 'Vide'}. Erreur : {parse_error}")
                            continue

                if imported_history:
                    imported_data = {
                        user_id: {
                            "name": "",
                            "username": "",
                            "history": imported_history
                        }
                    }
                    import_successful = True
                    await update.message.reply_text(
                        "⚠️ Tu es sur le point d'importer des données TXT. "
                        "Ceci remplacera TOUT ton historique actuel.\n"
                        "Note : Le format TXT n'inclut pas le nom et le pseudo, ceux de ton profil actuel seront conservés ou définis.\n"
                        "Réponds OUI pour confirmer, NON pour annuler.",
                        reply_markup=ReplyKeyboardMarkup([["OUI", "NON"]], resize_keyboard=True)
                    )
                else:
                    await update.message.reply_text("Aucune donnée valide trouvée dans le fichier TXT.", reply_markup=get_main_menu())

            except Exception as e:
                logging.error(f"Erreur lors de l'import TXT pour l'utilisateur {user_id}: {e}")
                await update.message.reply_text(f"Erreur lors de l'import TXT : {e}", reply_markup=get_main_menu())
        else:
            await update.message.reply_text("Merci d'envoyer un fichier au format .json, .csv ou .txt.", reply_markup=get_main_menu())

        if import_successful:
            context.user_data["imported_data_to_confirm"] = imported_data
            context.user_data["awaiting_import_confirmation"] = True
        else:
            context.user_data.pop("imported_data_to_confirm", None)
            context.user_data.pop("awaiting_import_confirmation", None)

    else:
        await update.message.reply_text(
            "Merci d'envoyer un fichier à importer (JSON, CSV ou TXT) juste après cette commande.",
            reply_markup=get_main_menu()
        )

    if context.user_data.get("awaiting_import_confirmation"):
        return
    else:
        return ConversationHandler.END

async def handle_import_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la confirmation d'importation des données."""
    if context.user_data.get("awaiting_import_confirmation"):
        response = update.message.text.strip().lower()
        if response == "oui":
            imported_data = context.user_data.get("imported_data_to_confirm")
            user_id = str(update.effective_user.id)

            if not imported_data or user_id not in imported_data:
                logging.error(f"Confirmation d'import reçue mais pas de données trouvées pour l'utilisateur {user_id}")
                await update.message.reply_text("Une erreur interne s'est produite. Importation annulée.", reply_markup=get_main_menu())
                context.user_data.pop("imported_data_to_confirm", None)
                context.user_data.pop("awaiting_import_confirmation", None)
                return ConversationHandler.END

            user_data_to_import = imported_data[user_id]
            history_to_import = user_data_to_import.get("history", [])
            imported_name = user_data_to_import.get("name", "")
            imported_username = user_data_to_import.get("username", "")

            conn = None
            try:
                conn = sqlite3.connect(DATABASE_FILE)
                cursor = conn.cursor()

                conn.execute("BEGIN TRANSACTION")

                cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
                logging.info(f"Historique existant supprimé pour l'utilisateur {user_id} avant import.")

                current_first_name = update.effective_user.first_name or ""
                current_last_name = update.effective_user.last_name or ""
                current_username = update.effective_user.username or ""
                current_full_name = f"{current_first_name} {current_last_name}".strip()

                name_to_save = imported_name if imported_name else current_full_name
                username_to_save = imported_username if imported_username else current_username

                cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
                user_exists = cursor.fetchone()

                if not user_exists:
                    cursor.execute("INSERT INTO users (user_id, name, username) VALUES (?, ?, ?)",
                                 (user_id, name_to_save, username_to_save))
                    logging.info(f"Utilisateur {user_id} créé pendant l'import.")
                else:
                    cursor.execute("UPDATE users SET name = ?, username = ? WHERE user_id = ?",
                                 (name_to_save, username_to_save, user_id))
                    logging.info(f"Informations utilisateur mises à jour pour {user_id} pendant l'import.")

                for entry in history_to_import:
                    type_ = entry.get("type", "-")
                    cote = entry.get("cote", "-")
                    case_number = entry.get("case", "-")
                    side = entry.get("side", "-")
                    side_ref = entry.get("side_ref", "")
                    resultat = entry.get("resultat", "-")
                    date = entry.get("date", "-")
                    heure = entry.get("heure", "-")
                    seconde = entry.get("seconde", "-")
                    bet_amount = entry.get("bet_amount", "-")


                    cursor.execute(
                        "INSERT INTO history (user_id, type, cote, case_number, side, side_ref, resultat, date, heure, seconde, bet_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (user_id, type_, cote, case_number, side, side_ref, resultat, date, heure, seconde, bet_amount)
                    )
                conn.commit()
                logging.info(f"{len(history_to_import)} entrées d'historique insérées pour l'utilisateur {user_id}.")

                await update.message.reply_text(
                    #f"{'✅' if type_ == 'gagne' else '❌'} Séquence enregistrée !",
                    f"✅ Importation réussie ! {len(history_to_import)} entrées d'historique ont été importées.",
                    reply_markup=get_main_menu()
                )

            except sqlite3.Error as e:
                logging.error(f"Erreur base de données pendant la confirmation d'import pour l'utilisateur {user_id}: {e}")
                if conn:
                    conn.execute("ROLLBACK")
                await update.message.reply_text(
                    "❌ Une erreur s'est produite lors de l'enregistrement de la séquence.",
                    reply_markup=get_main_menu()
                )
            finally:
                if conn:
                    conn.close()

        elif response == "non":
            context.user_data.pop("imported_data_to_confirm", None)
            context.user_data.pop("awaiting_import_confirmation", None)
            await update.message.reply_text("❌ Import annulé. Tes données précédentes sont intactes.", reply_markup=get_main_menu())
        else:
            await update.message.reply_text(
                "Merci de répondre par OUI ou NON.",
                reply_markup=ReplyKeyboardMarkup([["OUI", "NON"]], resize_keyboard=True)
            )
            return

    pass

async def predire_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    # Vérification des limites
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        hour_str = now.strftime("%Y-%m-%d %H")
        cursor.execute("SELECT predictions_today, last_prediction_day, predictions_hour, last_prediction_hour, predictions_total, limit_per_day, limit_per_hour, limit_total FROM user_access WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            predictions_today, last_day, predictions_hour, last_hour, predictions_total, limit_per_day, limit_per_hour, limit_total = row
            # Limites personnalisées ou globales
            limit_per_day = limit_per_day if limit_per_day is not None else MAX_PREDICTIONS_PER_DAY
            limit_per_hour = limit_per_hour if limit_per_hour is not None else MAX_PREDICTIONS_PER_HOUR
            limit_total = limit_total if limit_total is not None else MAX_PREDICTIONS_TOTAL
            # Reset si changement de jour/heure
            if last_day != today_str:
                predictions_today = 0
                last_day = today_str
            if last_hour != hour_str:
                predictions_hour = 0
                last_hour = hour_str
            # Vérification
            if predictions_today >= limit_per_day:
                await update.message.reply_text(f"🚫 Limite de prédictions par jour atteinte ({limit_per_day}).")
                return
            if predictions_hour >= limit_per_hour:
                await update.message.reply_text(f"🚫 Limite de prédictions par heure atteinte ({limit_per_hour}).")
                return
            if predictions_total >= limit_total:
                await update.message.reply_text(f"🚫 Limite totale de prédictions atteinte ({limit_total}).")
                return
            # Incrémentation
            predictions_today += 1
            predictions_hour += 1
            predictions_total += 1

        cursor.execute("UPDATE user_access SET predictions_today=?, last_prediction_day=?, predictions_hour=?, last_prediction_hour=?, predictions_total=? WHERE user_id=?", (predictions_today, last_day, predictions_hour, last_hour, predictions_total, user_id))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        await update.message.reply_text(f"Erreur lors de la vérification des limites : {e}")
        return
    finally:
        if conn:
            conn.close()
    if update.message.text and "prédire" in update.message.text.lower():
        context.user_data.pop("bet_amount", None)
    if "id_1xbet" not in context.user_data:
        await update.message.reply_text(
            "Pour une simulation personnalisée, entre ton ID utilisateur 1xbet, puis clique sur OK pour confirmer (ou NON pour une simulation totalement aléatoire).",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("OK")], [KeyboardButton("NON")]],
                resize_keyboard=True
            )
        )
        context.user_data["awaiting_id"] = True
        context.user_data["temp_id"] = ""
        return ASK_1XBET_ID

    user_id_1xbet = context.user_data.get("id_1xbet")
    bet_amount_for_rng = context.user_data.get("bet_amount")

    if bet_amount_for_rng is None:
        await update.message.reply_text(
            "Entre le montant de ton pari (ex: 100, 50.5) :",
            reply_markup=ReplyKeyboardMarkup([["200", "300", "400"], ["500", "750", "1000"]], resize_keyboard=True)
        )
        return ASK_BET_AMOUNT

    rng, seed_str = get_rng(user_id_1xbet, bet_amount_for_rng)
    context.user_data["auto_preds"] = []
    pred_msgs = []
    sides_ref = ["gauche", "droite"]

    seed_logs = []
    if user_id_1xbet or bet_amount_for_rng:
        seed_logs.append(f"🧮 Logs de calcul du seed :")
        seed_logs.append(f"Seed utilisé : {seed_str}")
        seed_components = []
        if user_id_1xbet:
            seed_components.append(f'"{user_id_1xbet}"')
        if bet_amount_for_rng is not None:
            seed_components.append(f'"{bet_amount_for_rng}"')
        if user_id_1xbet is not None or bet_amount_for_rng is not None:
            now = datetime.datetime.now()
            now_str_log = now.strftime("%Y%m%d_%H%M%S_%f")
            seed_components.append(f'"{now_str_log}"')

        log_seed = "_".join(c.strip("'\"") for c in seed_components)
        seed_logs.append(f'random = random.Random("{log_seed}")')

    for i, cote in enumerate(COTES):
        tirage_case = rng.choice([1, 2, 3, 4, 5])
        tirage_sens = rng.choice(sides_ref)
        case = str(tirage_case)
        side_ref = tirage_sens
        context.user_data["auto_preds"].append({"cote": cote, "case": case, "side_ref": side_ref})
        pred_msgs.append(
            f"Prédiction cote {cote} : sélectionne la case {case} (en comptant depuis la {side_ref})"
        )
        
        # Envoi de l'image correspondante
        image_path = os.path.join(IMAGES_DIR, f"case{case}_{side_ref}.{IMAGE_EXT}")
        
        if os.path.exists(image_path):
            with open(image_path, "rb") as img:
                await update.message.reply_photo(
                    photo=img,
                    caption=f"Case {case} ({side_ref}) pour la cote {cote}"
                )
        else:
            await update.message.reply_text(
                f"⚠️ Image non disponible pour la case {case} ({side_ref})"
            )

    if user_id_1xbet is not None or bet_amount_for_rng is not None:
        await update.message.reply_text(
            "\n".join(seed_logs)
        )
        await update.message.reply_text(
            "Voici la séquence calculée pour ce seed :\n" + "\n".join(pred_msgs)
        )
    else:
        await update.message.reply_text(
            "🍏 Séquence automatique (simulation 1xbet)\n\n" + "\n".join(pred_msgs)
        )

    await update.message.reply_text(
        "Après avoir joué sur 1xbet, indique si tu as GAGNÉ ou PERDU la séquence (gagné si tu as eu 'Bonne' pour les 2 cotes, sinon perdu).",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("🏆 Gagné"), KeyboardButton("💥 Perdu")]],
            resize_keyboard=True)
    )
    return ASK_RESULTS

async def ask_1xbet_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    text = update.message.text.strip()
    if text.upper() == "NON":
        context.user_data["id_1xbet"] = None
        context.user_data.pop("awaiting_id", None)
        context.user_data.pop("temp_id", None)
        await update.message.reply_text(
            "Entre le montant de ton pari (ex: 100, 50.5) :",
            reply_markup=ReplyKeyboardMarkup([["200", "300", "400"], ["500", "750", "1000"]], resize_keyboard=True)
        )
        return ASK_BET_AMOUNT
    elif text.upper() == "OK":
        user_id_input = context.user_data.get("temp_id", "").strip()
        if not user_id_input.isdigit() or len(user_id_input) != 10:
            await update.message.reply_text(
                "L'ID utilisateur 1xbet doit être composé de 10 chiffres. Merci de réessayer ou de taper NON pour annuler."
            )
            context.user_data["temp_id"] = ""
            return ASK_1XBET_ID

        context.user_data["id_1xbet"] = user_id_input
        context.user_data.pop("awaiting_id", None)
        context.user_data.pop("temp_id", None)
        await update.message.reply_text(
            "Entre le montant de ton pari (ex: 100, 50.5) :",
            reply_markup=ReplyKeyboardMarkup([["200", "300", "400"], ["500", "750", "1000"]], resize_keyboard=True)
        )
        return ASK_BET_AMOUNT
    else:
        if not text.isdigit() or len(text) != 10:
            await update.message.reply_text(
                "L'ID utilisateur 1xbet doit être composé de 10 chiffres. Merci de réessayer ou de taper NON pour annuler."
            )
            context.user_data["temp_id"] = ""
            return ASK_1XBET_ID
        else:
            context.user_data["temp_id"] = text
            await update.message.reply_text(
                f"ID entré : {text}\nClique sur OK pour confirmer ou NON pour annuler.",
                reply_markup=ReplyKeyboardMarkup(
                    [[KeyboardButton("OK")], [KeyboardButton("NON")]],
                    resize_keyboard=True
                )
            )
            return ASK_1XBET_ID

async def collect_bet_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    bet_amount_str = update.message.text.strip()
    try:
        bet_amount_float = float(bet_amount_str)
        if bet_amount_float <= 0:
            await update.message.reply_text("Merci d'entrer un montant de pari positif.")
            return ASK_BET_AMOUNT
        context.user_data["bet_amount"] = bet_amount_str
    except ValueError:
        await update.message.reply_text("Montant invalide. Merci d'entrer un nombre valide (ex: 100, 50.5).")
        return ASK_BET_AMOUNT

    return await predire_auto(update, context)

async def after_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    result_text = update.message.text.lower()
    if "gagné" in result_text or "gagne" in result_text:
        context.user_data['auto_result'] = "gagne"
    elif "perdu" in result_text:
        context.user_data['auto_result'] = "perdu"
    else:
        await update.message.reply_text("Merci de choisir 'Gagné' ou 'Perdu'.")
        return ASK_RESULTS

    context.user_data["auto_case_details"] = []
    context.user_data["auto_case_step"] = 0
    await update.message.reply_text(
        f"Pour la cote {COTES[0]}, sur quelle case étais-tu ? (1, 2, 3, 4 ou 5)",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(c) for c in POSITIONS]], resize_keyboard=True)
    )
    return ASK_CASES

async def collect_case(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    case = update.message.text.strip()
    if case not in POSITIONS:
        await update.message.reply_text("Merci d'entrer un numéro de case valide : 1, 2, 3, 4 ou 5.")
        return ASK_CASES

    step = context.user_data.get("auto_case_step", 0)
    side_ref = context.user_data.get("side_refs", [])[step] if step < len(context.user_data.get("side_refs", [])) else "?"
    context.user_data["auto_case_details"].append({"cote": COTES[step], "case": case, "side_ref": side_ref})
    context.user_data["auto_case_step"] = step + 1
    await update.message.reply_text(
        f"As-tu joué à GAUCHE ou à DROITE de la case {case} pour la cote {COTES[step]} (prédiction à compter depuis la {side_ref}) ?",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Gauche"), KeyboardButton("Droite")]], resize_keyboard=True)
    )
    return ASK_SIDE

async def collect_side(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    side = update.message.text.strip().capitalize()
    if side not in SIDES:
        await update.message.reply_text("Merci de répondre par 'Gauche' ou 'Droite'.")
        return ASK_SIDE

    step = context.user_data.get("auto_case_step", 1)
    if step > 0 and step-1 < len(context.user_data.get("auto_case_details", [])):
        context.user_data["auto_case_details"][step-1]["side"] = side
        await update.message.reply_text(
            f"La case {context.user_data['auto_case_details'][step-1]['case']} ({side}) pour la cote {COTES[step-1]}, était-elle Bonne ou Mauvaise ?",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Bonne"), KeyboardButton("Mauvaise")]], resize_keyboard=True)
        )
        return ASK_BONNE_MAUVAISE
    else:
        logging.error("Erreur dans collect_side: auto_case_step hors limites ou auto_case_details manquant.")
        await update.message.reply_text(
            "Une erreur interne s'est produite. Veuillez réessayer en cliquant sur '🍏 Prédire'.",
            reply_markup=get_main_menu()
        )
        return ConversationHandler.END

async def collect_bonne_mauvaise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    reponse = update.message.text.strip().lower()
    if reponse not in ["bonne", "mauvaise"]:
        await update.message.reply_text("Merci de répondre par 'Bonne' ou 'Mauvaise'.")
        return ASK_BONNE_MAUVAISE

    step = context.user_data.get("auto_case_step", 1)
    if step > 0 and step-1 < len(context.user_data.get("auto_case_details", [])):
        context.user_data["auto_case_details"][step-1]["resultat"] = reponse.capitalize()
    else:
        logging.error("Erreur dans collect_bonne_mauvaise: auto_case_step hors limites ou auto_case_details manquant.")
        await update.message.reply_text(
            "Une erreur interne s'est produite. Veuillez réessayer en cliquant sur '🍏 Prédire'.",
            reply_markup=get_main_menu()
        )
        context.user_data.pop("id_1xbet", None)
        context.user_data.pop("bet_amount", None)
        context.user_data.pop("auto_preds", None)
        context.user_data.pop("side_refs", None)
        context.user_data.pop("auto_case_details", None)
        context.user_data.pop("auto_case_step", None)
        context.user_data.pop("auto_result", None)
        return ConversationHandler.END

    if step < len(COTES):
        await update.message.reply_text(
            f"Pour la cote {COTES[step]}, sur quelle case étais-tu ? (1, 2, 3, 4 ou 5)",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton(c) for c in POSITIONS]], resize_keyboard=True)
        )
        return ASK_CASES

    user_id = str(update.effective_user.id)
    result_type = context.user_data.get('auto_result')
    timeinfo = current_time_data()
    bet_amount = context.user_data.get("bet_amount", "-")

    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        conn.execute("BEGIN TRANSACTION")

        for i, detail in enumerate(context.user_data.get("auto_case_details", [])):
            cote = detail.get("cote", "-")
            case = detail.get("case", "-")
            side = detail.get("side", "-")
            side_ref = detail.get("side_ref", "-")
            resultat = detail.get("resultat", "-")

            cursor.execute(
                "INSERT INTO history (user_id, type, cote, case_number, side, side_ref, resultat, date, heure, seconde, bet_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, result_type, cote, case, side, side_ref, resultat, timeinfo["date"], timeinfo["heure"], timeinfo["seconde"], bet_amount)
            )
        conn.commit()
        logging.info(f"Séquence enregistrée en base pour l'utilisateur {user_id}")

        await update.message.reply_text(
            f"{'✅' if result_type == 'gagne' else '❌'} Séquence enregistrée !",
            reply_markup=get_main_menu()
        )

    except sqlite3.Error as e:
        logging.error(f"Erreur base de données lors de l'enregistrement de la séquence pour l'utilisateur {user_id}: {e}")
        if conn:
            conn.execute("ROLLBACK")
        await update.message.reply_text(
            "❌ Une erreur s'est produite lors de l'enregistrement de la séquence.",
            reply_markup=get_main_menu()
        )
    finally:
        if conn:
            conn.close()

    context.user_data.pop("bet_amount", None)
    context.user_data.pop("auto_preds", None)
    context.user_data.pop("side_refs", None)
    context.user_data.pop("auto_case_details", None)
    context.user_data.pop("auto_case_step", None)
    context.user_data.pop("auto_result", None)

    return ASK_RESULTS


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère les clics sur les boutons du menu."""
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    text = update.message.text.strip().lower()

    if contains_scam_words(text):
        await update.message.reply_text(
            "❌ Il n'existe aucune astuce, hack, bot, ou méthode secrète pour gagner à Apple of Fortune. "
            "Le jeu sur 1xbet repose sur un hasard pur (RNG) : chaque case a exactement 20% de chance d'être gagnante à chaque tour. "
            "Méfie-toi des arnaques sur internet !",
            reply_markup=get_main_menu()
        )
        return
    elif "aide" in text:
        if int(user_id) == ADMIN_TELEGRAM_ID:
            await admin_help(update, context)
        else:
            await user_help(update, context)
        return
    elif "importer" in text:
        await update.message.reply_text(
            "Merci d'envoyer le fichier JSON, CSV ou TXT que tu veux importer, via le trombone (📎).",
            reply_markup=get_main_menu()
        )
    elif "fonctionnement" in text:
        await fonctionnement(update, context)
    elif "conseils" in text:
        await conseils(update, context)
    elif "arnaques" in text:
        await arnaques(update, context)
    elif "contact" in text:
        await contact(update, context)
    elif "faq" in text:
        await faq(update, context)
    elif "tutoriel" in text:
        await tuto(update, context)
    elif "à propos" in text or "a propos" in text:
        await apropos(update, context)
    elif "historique" in text:
        await historique(update, context)
    elif "statistique" in text or "statistic" in text:
        await stats_perso(update, context)
    elif "⬅️ menu principal" in text:
        await update.message.reply_text("Retour au menu principal.", reply_markup=get_main_menu())
    else:
        await update.message.reply_text(
            "Commande inconnue. Utilise le menu en bas ou tape /start.",
            reply_markup=get_main_menu()
        )

async def reset_choix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réinitialise les choix de prédiction de l'utilisateur."""
    for key in [
        "id_1xbet", "bet_amount", "auto_preds", "side_refs", "auto_case_details", "auto_case_step", "auto_result",
        "awaiting_id", "temp_id"
    ]:
        context.user_data.pop(key, None)
    await update.message.reply_text(
        "✅ Tes choix de prédiction ont été réinitialisés. Tu peux recommencer une nouvelle prédiction !",
        reply_markup=get_main_menu()
    )

async def cancel_and_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Opération annulée.", reply_markup=get_main_menu())
    return ConversationHandler.END

# Fonction utilitaire pour vérifier l'accès
def check_access(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("SELECT expiration, suspended FROM user_access WHERE user_id = ? AND expiration > ?", (str(user_id), now))
        row = cursor.fetchone()
        if row:
            expiration, suspended = row
            return bool(expiration) and (suspended is None or suspended == 0)
        return False
    except Exception as e:
        logging.error(f"Erreur check_access: {e}")
        return False
    finally:
        if conn:
            conn.close()

# Commande admin pour générer un code
async def gen_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut générer des codes d'accès.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Utilisation : /gen_code <user_id> <minutes>")
        return
    for_user_id, minutes = context.args
    try:
        minutes = int(minutes)
    except ValueError:
        await update.message.reply_text("La durée doit être un nombre de minutes.")
        return
    code = secrets.token_hex(4)
    expiration = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO access_codes (code, for_user_id, expiration, used) VALUES (?, ?, ?, 0)", (code, for_user_id, expiration))
        conn.commit()
        await update.message.reply_text(f"Code généré pour l'utilisateur {for_user_id} :\n<code>{code}</code>\nValable jusqu'à : {expiration}", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la génération du code : {e}")
    finally:
        if conn:
            conn.close()

# Commande utilisateur pour activer un code
async def activate_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /activate <code>")
        return
    code = context.args[0]
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Vérifie que le code existe, n'est pas utilisé, n'est pas expiré, et correspond à ce user_id
        cursor.execute("SELECT expiration, used, for_user_id FROM access_codes WHERE code = ?", (code,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("❌ Code invalide.")
            return
        expiration, used, for_user_id = row
        if used:
            await update.message.reply_text("❌ Ce code a déjà été utilisé.")
            return
        if for_user_id != user_id:
            await update.message.reply_text("❌ Ce code n'est pas destiné à ton compte Telegram.")
            return
        if expiration < now:
            await update.message.reply_text("❌ Ce code est expiré.")
            return
        # Marque le code comme utilisé
        cursor.execute("UPDATE access_codes SET used = 1 WHERE code = ?", (code,))
        # Ajoute ou met à jour l'accès utilisateur
        cursor.execute("INSERT OR REPLACE INTO user_access (user_id, expiration) VALUES (?, ?)", (user_id, expiration))
        conn.commit()
        await update.message.reply_text(f"✅ Accès activé ! Valide jusqu'au : {expiration}")
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de l'activation : {e}")
    finally:
        if conn:
            conn.close()


# Commande admin pour afficher la structure de la base
async def db_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        def format_table_info(table_name, info):
            msg = f"\nStructure de {table_name} :\n"
            msg += f"{'cid':<3} {'name':<18} {'type':<12} {'notnull':<7} {'dflt_value':<15} {'pk':<3}\n"
            msg += "-"*65 + "\n"
            for col in info:
                cid, name, coltype, notnull, dflt_value, pk = col[:6]
                msg += f"{cid:<3} {name:<18} {coltype:<12} {notnull!s:<7} {str(dflt_value):<15} {pk:<3}\n"
            return msg
        access_codes_info = cursor.execute("PRAGMA table_info(access_codes)").fetchall()
        user_access_info = cursor.execute("PRAGMA table_info(user_access)").fetchall()
        users_info = cursor.execute("PRAGMA table_info(users)").fetchall()
        history_info = cursor.execute("PRAGMA table_info(history)").fetchall()
        msg = "\n".join([
            format_table_info("access_codes", access_codes_info),
            format_table_info("user_access", user_access_info),
            format_table_info("users", users_info),
            format_table_info("history", history_info)
        ])
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la lecture de la base : {e}")
    finally:
        if conn:
            conn.close()

# Commande admin pour lister tous les utilisateurs ayant un accès
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ua.user_id, u.name, u.username, ua.expiration, ua.suspended
            FROM user_access ua
            LEFT JOIN users u ON ua.user_id = u.user_id
            ORDER BY ua.expiration DESC
        """)
        rows = cursor.fetchall()
        if not rows:
            await update.message.reply_text("Aucun utilisateur avec accès trouvé.")
            return
        msg = "Liste des utilisateurs :\n"
        for user_id, name, username, expiration, suspended in rows:
            statut = "Actif" if (suspended is None or suspended == 0) else "Suspendu"
            name = name or "-"
            username = f"@{username}" if username else "-"
            msg += f"- {user_id} | {name} ({username}) | Expire : {expiration} | {statut}\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la récupération des utilisateurs : {e}")
    finally:
        if conn:
            conn.close()

# Commande pour afficher toutes les commandes utilisateur disponibles
async def user_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if int(user_id) != ADMIN_TELEGRAM_ID and not check_access(user_id):
        await update.message.reply_text("⛔️ Accès refusé. Merci de demander un code d'accès à l'administrateur.")
        return
    msg = (
        "📋 Commandes utilisateur disponibles :\n"
        "\n"
        "/start\n"
        "  Affiche le menu principal du bot.\n"
        "/fonctionnement\n"
        "  Explication du fonctionnement du jeu et du bot.\n"
        "/conseils\n"
        "  Conseils pour jouer de façon responsable.\n"
        "/arnaques\n"
        "  Mise en garde contre les arnaques.\n"
        "/contact\n"
        "  Informations de contact et aide.\n"
        "/faq\n"
        "  Foire aux questions.\n"
        "/tuto\n"
        "  Tutoriel rapide pour utiliser le bot.\n"
        "/apropos\n"
        "  À propos du bot.\n"
        "/historique\n"
        "  Voir ton historique de parties.\n"
        "/statistiques ou /stats\n"
        "  Voir tes statistiques personnelles.\n"
        "/import\n"
        "  Importer un historique.\n"
        "Boutons du menu :\n"
        "  🍏 Prédire : Lancer une prédiction.\n"
        "  📤 Exporter : Exporter ton historique.\n"
        "  📥 Importer : Importer un historique.\n"
        "  ♻️ Réinitialiser historique : Supprimer tout ton historique.\n"
        "  🔄 Réinitialiser choix : Réinitialiser tes choix de prédiction.\n"
    )
    await update.message.reply_text(msg)

# Commande admin pour afficher toutes les commandes admin disponibles
async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    msg = (
        "🛡 Commandes administrateur disponibles :\n"
        "\n"
        "/gen_code <user_id> <minutes>\n"
        "  Génère un code d'accès temporaire pour un utilisateur.\n"
        "/db_info\n"
        "  Affiche la structure des tables d'accès.\n"
        "/list_users\n"
        "  Affiche la liste de tous les utilisateurs ayant un accès.\n"
        "/list_all_users\n"
        "  Affiche la liste de tous les utilisateurs enregistrés (même sans accès).\n"
        "/export_all_users\n"
        "  Exporte la liste de tous les utilisateurs (user_id, nom, username) en TXT.\n"
        "/suspend_user <user_id>\n"
        "  Suspend l'accès d'un utilisateur (le bloque).\n"
        "/unsuspend_user <user_id>\n"
        "  Réactive l'accès d'un utilisateur suspendu.\n"
        "/extend_access <user_id> <minutes>\n"
        "  Prolonge la durée d'accès d'un utilisateur.\n"
        "/reduce_access <user_id> <minutes>\n"
        "  Réduit la durée d'accès d'un utilisateur.\n"
        "/set_access <user_id> <YYYY-MM-DD HH:MM:SS>\n"
        "  Définit une nouvelle date d'expiration pour l'accès d'un utilisateur.\n"
        "/set_limit <user_id> <par_jour> <par_heure> <total>\n"
        "  Définit des limites personnalisées de prédiction pour un utilisateur.\n"
        "/user_status <user_id>\n"
        "  Affiche le statut, les quotas et les 10 dernières prédictions d'un utilisateur.\n"
        "/user_history <user_id>\n"
        "  Exporte tout l'historique d'un utilisateur en TXT.\n"
        "/user_email <user_id>\n"
        "  Exporte l'email, le nom et le username d'un utilisateur en TXT.\n"
        "\n"
        "\U0001F4BE /backup_db\n"
        "  Sauvegarde manuelle de la base de données (fichier .db envoyé à l'admin).\n"
        "/restore_db\n"
        "  Restaure la base de données à partir d'un fichier .db (nécessite confirmation).\n"
        "\n"
        "\U0001F4C1 Sauvegarde automatique :\n"
        "  À chaque démarrage, une sauvegarde de la base est créée dans le dossier backups/.\n"
        "  Les 20 dernières sauvegardes sont conservées automatiquement.\n"
        "\n"
        "Mise à jour automatique :\n"
        "  Le nom et le username de chaque utilisateur sont désormais mis à jour à chaque interaction (message ou bouton).\n"
    )
    await update.message.reply_text(msg)

# Commande admin pour suspendre un utilisateur
async def suspend_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /suspend_user <user_id>")
        return
    user_id = context.args[0]
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE user_access SET suspended = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        await update.message.reply_text(f"Utilisateur {user_id} suspendu (bloqué).")
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la suspension : {e}")
    finally:
        if conn:
            conn.close()

# Commande admin pour réactiver un utilisateur suspendu
async def unsuspend_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /unsuspend_user <user_id>")
        return
    user_id = context.args[0]
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE user_access SET suspended = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        await update.message.reply_text(f"Utilisateur {user_id} réactivé.")
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la réactivation : {e}")
    finally:
        if conn:
            conn.close()
# Commande admin pour prolonger la durée d'accès d'un utilisateur
async def extend_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Utilisation : /extend_access <user_id> <minutes>")
        return
    user_id, minutes = context.args
    try:
        minutes = int(minutes)
    except ValueError:
        await update.message.reply_text("Le nombre de minutes doit être un entier.")
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT expiration FROM user_access WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("Utilisateur non trouvé.")
            return
        expiration = row[0]
        new_exp = (datetime.datetime.strptime(expiration, "%Y-%m-%d %H:%M:%S") + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE user_access SET expiration = ? WHERE user_id = ?", (new_exp, user_id))
        conn.commit()
        await update.message.reply_text(f"Accès de {user_id} prolongé jusqu'au {new_exp}.")
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la prolongation : {e}")
    finally:
        if conn:
            conn.close()

# Commande admin pour réduire la durée d'accès d'un utilisateur
async def reduce_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Utilisation : /reduce_access <user_id> <minutes>")
        return
    user_id, minutes = context.args
    try:
        minutes = int(minutes)
    except ValueError:
        await update.message.reply_text("Le nombre de minutes doit être un entier.")
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT expiration FROM user_access WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("Utilisateur non trouvé.")
            return
        expiration = row[0]
        new_exp = (datetime.datetime.strptime(expiration, "%Y-%m-%d %H:%M:%S") - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE user_access SET expiration = ? WHERE user_id = ?", (new_exp, user_id))
        conn.commit()
        await update.message.reply_text(f"Accès de {user_id} réduit jusqu'au {new_exp}.")
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la réduction : {e}")
    finally:
        if conn:
            conn.close()
# Commande admin pour fixer une nouvelle date d'expiration
async def set_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Utilisation : /set_access <user_id> <YYYY-MM-DD HH:MM:SS>")
        return
    user_id, new_exp = context.args
    try:
        # Vérifie le format de la date
        datetime.datetime.strptime(new_exp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        await update.message.reply_text("Format de date invalide. Utilise : YYYY-MM-DD HH:MM:SS")
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE user_access SET expiration = ? WHERE user_id = ?", (new_exp, user_id))
        conn.commit()
        await update.message.reply_text(f"Nouvelle date d'expiration pour {user_id} : {new_exp}")
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la modification : {e}")
    finally:
        if conn:
            conn.close()

# Commande admin pour définir les limites personnalisées d'un utilisateur
async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 4:
        await update.message.reply_text("Utilisation : /set_limit <user_id> <par_jour> <par_heure> <total>")
        return
    user_id, per_day, per_hour, total = context.args
    try:
        per_day = int(per_day)
        per_hour = int(per_hour)
        total = int(total)
    except ValueError:
        await update.message.reply_text("Les limites doivent être des entiers.")
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE user_access SET limit_per_day=?, limit_per_hour=?, limit_total=? WHERE user_id=?", (per_day, per_hour, total, user_id))
        conn.commit()
        await update.message.reply_text(f"Limites personnalisées pour {user_id} : {per_day}/jour, {per_hour}/heure, {total}/total.")
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la modification des limites : {e}")
    finally:
        if conn:
            conn.close()

# Commande admin pour surveiller un utilisateur
async def user_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /user_status <user_id>")
        return
    user_id = context.args[0]
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        # Infos accès
        cursor.execute("""
            SELECT expiration, suspended, predictions_today, predictions_hour, predictions_total,
                   limit_per_day, limit_per_hour, limit_total
            FROM user_access WHERE user_id = ?
        """, (user_id,))
        access = cursor.fetchone()
        if not access:
            await update.message.reply_text("Utilisateur non trouvé dans user_access.")
            return
        expiration, suspended, pred_today, pred_hour, pred_total, lim_day, lim_hour, lim_total = access
        statut = "Actif" if (suspended is None or suspended == 0) else "Suspendu"
        lim_day = lim_day if lim_day is not None else MAX_PREDICTIONS_PER_DAY
        lim_hour = lim_hour if lim_hour is not None else MAX_PREDICTIONS_PER_HOUR
        lim_total = lim_total if lim_total is not None else MAX_PREDICTIONS_TOTAL

        msg = (
            f"👤 Statut de l'utilisateur {user_id}\n"
            f"- Expiration : {expiration}\n"
            f"- Statut : {statut}\n"
            f"- Prédictions aujourd'hui : {pred_today}/{lim_day}\n"
            f"- Prédictions cette heure : {pred_hour}/{lim_hour}\n"
            f"- Prédictions totales : {pred_total}/{lim_total}\n"
        )

        # Historique (10 dernières séquences)
        cursor.execute("""
            SELECT type, cote, case_number, side, resultat, date, heure
            FROM history WHERE user_id = ?
            ORDER BY history_id DESC LIMIT 10
        """, (user_id,))
        rows = cursor.fetchall()
        if rows:
            msg += "\n🧠 10 dernières prédictions :\n"
            for row in rows:
                msg += f"- [{row[5]} {row[6]}] {row[1]}: case {row[2]} ({row[3]}) — {row[4]} ({row[0]})\n"
        else:
            msg += "\nAucun historique trouvé."

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Erreur : {e}")
    finally:
        if conn:
            conn.close()

# Commande admin pour exporter tout l'historique d'un utilisateur
async def user_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /user_history <user_id>")
        return
    user_id = context.args[0]
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT type, cote, case_number, side, side_ref, resultat, date, heure, seconde, bet_amount
            FROM history WHERE user_id = ?
            ORDER BY history_id
        """, (user_id,))
        rows = cursor.fetchall()
        if not rows:
            await update.message.reply_text("Aucun historique trouvé pour cet utilisateur.")
            return
        # Format TXT lisible, 2 lignes par séquence
        import io
        txt = io.StringIO()
        for i in range(0, len(rows), 2):
            try:
                a = rows[i]
                b = rows[i+1]
            except IndexError:
                continue
            date = a[6] if a[6] else "-"
            heure = a[7] if a[7] else "-"
            sec = a[8] if a[8] else "-"
            bet_amount = a[9] if a[9] else "-"
            case123 = a[2] if a[2] else "?"
            sens123 = a[3] if a[3] else "?"
            res123 = a[5] if a[5] else "?"
            case154 = b[2] if b[2] else "?"
            sens154 = b[3] if b[3] else "?"
            res154 = b[5] if b[5] else "?"
            etat = "🏆" if a[0] == "gagne" else "💥"
            txt.write(f"📅 {date} à {heure}:{sec} | Mise : {bet_amount}\n")
            txt.write(f"1️⃣ Cote 1.23 : Case {case123} ({sens123}) — {res123}\n")
            txt.write(f"2️⃣ Cote 1.54 : Case {case154} ({sens154}) — {res154}\n")
            txt.write(f"Résultat : {etat}\n")
            txt.write(f"--------------------\n")
        txt.seek(0)
        await update.message.reply_document(
            document=io.BytesIO(txt.getvalue().encode("utf-8")),
            filename=f"user_{user_id}_history.txt"
        )
        await update.message.reply_text("✅ Historique complet envoyé en TXT.")
    except Exception as e:
        await update.message.reply_text(f"Erreur : {e}")
    finally:
        if conn:
            conn.close()

async def user_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /user_email <user_id>")
        return
    user_id = context.args[0]
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT email, name, username FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("Aucun utilisateur trouvé.")
            return
        email, name, username = row
        if not email:
            email = "Non renseigné"
        if not name:
            name = "Non renseigné"
        if not username:
            username = "Non renseigné"
        import io
        txt = io.StringIO()
        txt.write(f"Email de l'utilisateur {user_id} : {email}\n")
        txt.write(f"Nom : {name}\n")
        txt.write(f"Username : {username}\n")
        txt.seek(0)
        await update.message.reply_document(
            document=io.BytesIO(txt.getvalue().encode("utf-8")),
            filename=f"user_{user_id}_info.txt"
        )
        await update.message.reply_text("✅ Email, nom et username exportés en .txt.")
    except Exception as e:
        await update.message.reply_text(f"Erreur : {e}")
    finally:
        if conn:
            conn.close()

# === Commande admin pour sauvegarder la base de données ===
async def backup_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    if not os.path.exists(DATABASE_FILE):
        await update.message.reply_text("Fichier de base de données introuvable.")
        return
    try:
        await update.message.reply_document(document=open(DATABASE_FILE, "rb"), filename=DATABASE_FILE)
        await update.message.reply_text("✅ Sauvegarde de la base envoyée.")
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de l'envoi de la base : {e}")

# === Commande admin pour restaurer la base de données ===
async def restore_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔️ Seul l'administrateur peut utiliser cette commande.")
        return
    await update.message.reply_text(
        "Merci d'envoyer le fichier .db à restaurer (nommé apple_predictor.db), via le trombone (📎).\nATTENTION : Cela remplacera toute la base actuelle après confirmation.",
        reply_markup=ReplyKeyboardMarkup([["Annuler restauration"]], resize_keyboard=True)
    )
    context.user_data["awaiting_db_restore_file"] = True

# === Handler pour réception d'un fichier .db pour restauration ===
async def handle_db_restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.user_data.get("awaiting_db_restore_file"):
        return
    if not update.message.document or not update.message.document.file_name.endswith(".db"):
        await update.message.reply_text("Merci d'envoyer un fichier .db valide.")
        return
    file = await update.message.document.get_file()
    temp_db_path = "restore_temp_apple_predictor.db"
    try:
        await file.download_to_drive(temp_db_path)
        context.user_data["restore_db_file_path"] = temp_db_path
        await update.message.reply_text(
            "⚠️ Es-tu sûr de vouloir restaurer la base de données avec ce fichier ? Cela remplacera TOUTES les données actuelles. Réponds OUI pour confirmer, NON pour annuler.",
            reply_markup=ReplyKeyboardMarkup([["OUI", "NON"]], resize_keyboard=True)
        )
        context.user_data["awaiting_db_restore_confirm"] = True
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de la réception du fichier : {e}")
        context.user_data.pop("awaiting_db_restore_file", None)

# === Handler pour confirmation de restauration de la base ===
async def handle_db_restore_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.user_data.get("awaiting_db_restore_confirm"):
        return
    response = update.message.text.strip().lower()
    if response == "oui":
        temp_db_path = context.user_data.get("restore_db_file_path")
        if not temp_db_path or not os.path.exists(temp_db_path):
            await update.message.reply_text("Fichier temporaire introuvable. Annulation.")
            context.user_data.pop("awaiting_db_restore_confirm", None)
            context.user_data.pop("awaiting_db_restore_file", None)
            return
        try:
            # Sauvegarde l'ancienne base
            if os.path.exists(DATABASE_FILE):
                shutil.copy2(DATABASE_FILE, DATABASE_FILE + ".bak")
            shutil.move(temp_db_path, DATABASE_FILE)
            await update.message.reply_text("✅ Base restaurée avec succès ! L'ancienne base a été sauvegardée en .bak.", reply_markup=get_main_menu())
        except Exception as e:
            await update.message.reply_text(f"Erreur lors de la restauration : {e}")
        finally:
            context.user_data.pop("awaiting_db_restore_confirm", None)
            context.user_data.pop("awaiting_db_restore_file", None)
            context.user_data.pop("restore_db_file_path", None)
    elif response == "non":
        # Annule la restauration
        temp_db_path = context.user_data.get("restore_db_file_path")
        if temp_db_path and os.path.exists(temp_db_path):
            os.remove(temp_db_path)
        await update.message.reply_text("❌ Restauration annulée.", reply_markup=get_main_menu())
        context.user_data.pop("awaiting_db_restore_confirm", None)
        context.user_data.pop("awaiting_db_restore_file", None)
        context.user_data.pop("restore_db_file_path", None)
    else:
        await update.message.reply_text("Merci de répondre par OUI ou NON.", reply_markup=ReplyKeyboardMarkup([["OUI", "NON"]], resize_keyboard=True))

# Fonction utilitaire pour mettre à jour le nom et le username à chaque interaction
async def update_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    user_id = str(user.id)
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = user.username or ""
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE users SET name = ?, username = ? WHERE user_id = ?", (name, username, user_id))
        else:
            cursor.execute("INSERT INTO users (user_id, name, username) VALUES (?, ?, ?)", (user_id, name, username))
        conn.commit()
    except Exception as e:
        logger.error(f"Erreur lors de la mise à jour du nom utilisateur {user_id}: {e}")
    finally:
        if conn:
            conn.close()

# Wrapper pour tous les handlers texte/bouton pour mettre à jour le nom à chaque interaction
async def handle_button_with_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_user_info(update, context)
    await handle_button(update, context)

def main():
    # Vérifier que le dossier des images existe
    if not os.path.exists(IMAGES_DIR):
        os.makedirs(IMAGES_DIR)
        logger.warning(f"Dossier {IMAGES_DIR} créé. Veuillez y placer vos images.")
    
    # Log du chemin de la base et vérification de sa taille
    db_path = os.path.abspath(DATABASE_FILE)
    print(f"Base de données utilisée : {db_path}")
    if not os.path.exists(DATABASE_FILE):
        print("\033[91mATTENTION : La base de données n'existait pas, elle va être créée.\033[0m")
    else:
        db_size = os.path.getsize(DATABASE_FILE)
        if db_size < 10 * 1024:
            print(f"\033[91mATTENTION : La base de données est très petite ({db_size} octets). Il est possible qu'elle soit vide ou corrompue.\033[0m")
    
    # === Sauvegarde automatique de la base de données ===
    BACKUP_DIR = os.path.join(SCRIPT_DIR, "backups")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if os.path.exists(DATABASE_FILE):
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"apple_predictor_{now_str}.db")
        shutil.copy2(DATABASE_FILE, backup_path)
        print(f"\033[92mSauvegarde automatique : {backup_path}\033[0m")
        # Limite à 20 sauvegardes
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "apple_predictor_*.db")))
        if len(backups) > 20:
            for old_backup in backups[:-20]:
                try:
                    os.remove(old_backup)
                    print(f"Suppression ancienne sauvegarde : {old_backup}")
                except Exception as e:
                    print(f"Erreur suppression sauvegarde {old_backup} : {e}")
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    init_db()  # Initialise la base de données au démarrage
    application = ApplicationBuilder().token(TOKEN).build()

    # Handler pour le bouton "🔄 Réinitialiser choix"
    application.add_handler(MessageHandler(filters.Regex("^(🔄 Réinitialiser choix|reinitialiser choix|réinitialiser choix)$"), reset_choix))

    # Commandes classiques
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("fonctionnement", fonctionnement))
    application.add_handler(CommandHandler("conseils", conseils))
    application.add_handler(CommandHandler("arnaques", arnaques))
    application.add_handler(CommandHandler("contact", contact))
    application.add_handler(CommandHandler("faq", faq))
    application.add_handler(CommandHandler("tuto", tuto))
    application.add_handler(CommandHandler("apropos", apropos))
    application.add_handler(CommandHandler("historique", historique))
    application.add_handler(CommandHandler("statistiques", stats_perso))
    application.add_handler(CommandHandler("stats", stats_perso))
    application.add_handler(CommandHandler("import", import_data))

# ConversationHandler pour la prédiction automatique
    auto_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^(🍏 Prédire|prédire|predire)$"), predire_auto),
        ],
        states={
            ASK_1XBET_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_1xbet_id)],
            ASK_BET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_bet_amount)],
            ASK_RESULTS: [MessageHandler(filters.Regex("^(🏆 Gagné|💥 Perdu|gagné|perdu|gagne)$"), after_result)],
            ASK_CASES: [MessageHandler(filters.Regex("^[1-5]$"), collect_case)],
            ASK_SIDE: [MessageHandler(filters.Regex("^(Gauche|Droite|gauche|droite)$"), collect_side)],
            ASK_BONNE_MAUVAISE: [MessageHandler(filters.Regex("^(Bonne|Mauvaise|bonne|mauvaise)$"), collect_bonne_mauvaise)],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^(⬅️ Menu principal|menu principal)$"), cancel_and_end),
            MessageHandler(filters.TEXT | filters.COMMAND, cancel_and_end)
        ],
        allow_reentry=True,
        name="auto_pred_conversation",
        persistent=False
    )
    application.add_handler(auto_conv)

    # ConversationHandler pour la réinitialisation de l'historique
    reset_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^(♻️ Réinitialiser historique|réinitialiser historique|reinitialiser historique)$"), reset_historique)
        ],
        states={
            RESET_CONFIRM: [MessageHandler(filters.Regex("^(OUI|NON|oui|non)$"), handle_reset_confirm)]
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^(⬅️ Menu principal|menu principal)$"), cancel_and_end),
            MessageHandler(filters.TEXT | filters.COMMAND, cancel_and_end)
        ],
        allow_reentry=True,
        name="reset_history_conversation",
        persistent=False
    )
    application.add_handler(reset_conv)

    # ConversationHandler pour le choix du format d'export
    export_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^(📤 Exporter|exporter)$"), ask_export_format)
        ],
        states={
            ASK_EXPORT_FORMAT: [MessageHandler(filters.Regex("^(JSON|CSV|TXT|⬅️ Menu principal|menu principal)$"), handle_export_format_choice)]
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT | filters.COMMAND, cancel_and_end)
        ],
        allow_reentry=True,
        name="export_conversation",
        persistent=False
    )
    application.add_handler(export_conv)

    # Handler pour les documents (import)
    application.add_handler(MessageHandler(filters.Document.ALL, import_data))
    # Handler pour la confirmation d'import (OUI/NON)
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(OUI|NON|oui|non)$"), handle_import_confirmation))

    # Handler général pour les boutons et textes (toujours en dernier)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_button_with_update))

    # Ajout des commandes admin
    application.add_handler(CommandHandler("gen_code", gen_code))
    application.add_handler(CommandHandler("activate", activate_code))
    application.add_handler(CommandHandler("db_info", db_info))
    application.add_handler(CommandHandler("list_users", list_users))
    application.add_handler(CommandHandler("admin_help", admin_help))
    application.add_handler(CommandHandler("user_help", user_help))
    application.add_handler(CommandHandler("suspend_user", suspend_user))
    application.add_handler(CommandHandler("unsuspend_user", unsuspend_user))
    application.add_handler(CommandHandler("extend_access", extend_access))
    application.add_handler(CommandHandler("reduce_access", reduce_access))
    application.add_handler(CommandHandler("set_access", set_access))
    application.add_handler(CommandHandler("set_limit", set_limit))
    application.add_handler(CommandHandler("user_status", user_status))
    application.add_handler(CommandHandler("user_history", user_history))
    application.add_handler(CommandHandler("user_email", user_email))
    application.add_handler(CommandHandler("backup_db", backup_db))
    application.add_handler(CommandHandler("restore_db", restore_db))
    # Handler pour réception d'un fichier .db pour restauration
    application.add_handler(MessageHandler(filters.Document.ALL, handle_db_restore_file))
    # Handler pour confirmation de restauration
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(OUI|NON|oui|non)$"), handle_db_restore_confirm))

    print("Bot démarré et base de données initialisée...")
    application.run_polling()

if __name__ == "__main__":
    main()
