import customtkinter as ctk
from tkinter import filedialog, messagebox, Toplevel
from playwright.sync_api import sync_playwright
import time
import calendar
import os
import shutil
import random
import threading
import re 
from datetime import datetime, date, timedelta

# --- IMPORTS IA ---
import whisper
import google.generativeai as genai

# --- CONFIGURATION API MULTI-CLÉS ---
GEMINI_API_KEYS = [
    "REDACTED_GEMINI_KEY",
    "REDACTED_GEMINI_KEY",
    "REDACTED_GEMINI_KEY",
    "REDACTED_GEMINI_KEY"
]

# --- CONFIGURATION FICHIERS ---
CHROME_DATA = r"E:\Tiktok\Chrome_Automation_Data"
ACCOUNTS = {
    "CompteA": "Profile 4",
    "CompteB": "Profile 2",
    "MotivationQuotes": "Profile 3",
    "CatFail": "Profile 5",
    "TwitchClippeur": "Profile 6"
}

OPTIMAL_TIMES = {
    0: ["06:00", "10:00", "18:00", "22:00"],
    1: ["07:00", "12:00", "18:00", "20:00"],
    2: ["08:00", "13:00", "16:00", "21:00"],
    3: ["05:00", "09:00", "12:00", "19:00"],
    4: ["07:00", "13:00", "15:00", "18:00"],
    5: ["09:00", "11:00", "19:00", "21:00"],
    6: ["08:00", "14:00", "18:00", "22:00"]
}

# --- PROMPT IA ---
GEMINI_PROMPT_BASE = """Agis comme un expert Senior en Algorithme TikTok et en SEO.

CONTEXTE :
- Compte cible : {account}
- Nom du fichier : "{filename}"
- Transcription audio : "{transcription}"

RÈGLES DE LANGUE (CRITIQUE) :
1. SI le compte cible est "MotivationQuotes" -> LE TITRE ET LES HASHTAGS DOIVENT ÊTRE 100% EN ANGLAIS.
2. SINON -> LE TITRE ET LES HASHTAGS DOIVENT ÊTRE EN FRANÇAIS.

TA MISSION :
1. Analyse le contenu. Si tu identifies un Streamer connu (Squeezie, Kameto, Amine, etc.) via le nom du fichier ou l'audio, mentionne-le ou utilise-le pour le contexte.
2. FALLBACK (IMPORTANT) : Si tu ne trouves AUCUN nom de streamer, CE N'EST PAS GRAVE. Génère simplement un titre viral générique basé sur l'action, l'humour ou le sujet de la vidéo.

RÈGLE DE FORME :
- Pas d'emojis.
- Pas de hashtags #fyp, #pourtoi, #viral.

FORMAT DE SORTIE STRICT :
[Titre de la vidéo]

[#Hashtag1 #Hashtag2 #Hashtag3 #Hashtag4 #Hashtag5]

Note: Fais deux sauts de ligne entre le titre et les hashtags.
"""

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# --- GESTION LOGS CONSOLE ---
def log_msg(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")

# --- CLASSE DE GESTION API (ROTATION) ---
class GeminiManager:
    def __init__(self, keys):
        self.keys = keys
        self.current_index = 0
    
    def get_content(self, prompt):
        attempts = 0
        while attempts < len(self.keys):
            current_key = self.keys[self.current_index]
            log_msg(f"[IA] Tentative avec clé API index {self.current_index} (finissant par ...{current_key[-6:]})")
            try:
                genai.configure(api_key=current_key)
                model = genai.GenerativeModel('gemini-2.5-flash')
                response = model.generate_content(prompt)
                log_msg("[IA] Réponse reçue avec succès.")
                return response.text
            except Exception as e:
                log_msg(f"[IA] ERREUR Clé index {self.current_index}: {e}")
                self.current_index = (self.current_index + 1) % len(self.keys)
                attempts += 1
                log_msg("[IA] Basculement vers la clé suivante...")
                time.sleep(1)
        raise Exception("Toutes les clés API ont échoué (Quota ou Erreur).")

# --- UI PRINCIPALE ---
class TikTokUploaderUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("TikTok Manager - FULL LOGS Edition")
        self.geometry("1150x850")
        
        self.selected_account = None
        self.video_path = ""
        self.account_buttons = {}

        self.gemini_manager = GeminiManager(GEMINI_API_KEYS)
        self.whisper_model = None 

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- SIDEBAR ---
        self.sidebar = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color="#1a1a1a")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
       
        self.logo = ctk.CTkLabel(self.sidebar, text="TIKTOK\nSTUDIO", font=ctk.CTkFont(size=22, weight="bold"), text_color="#FE2C55")
        self.logo.pack(pady=30)

        for acc in ACCOUNTS.keys():
            btn = ctk.CTkButton(self.sidebar, text=acc, height=40, font=("Segoe UI", 13),
                               fg_color="transparent", border_width=1, border_color="#3d3d3d",
                               hover_color="#FE2C55", command=lambda a=acc: self.select_account(a))
            btn.pack(pady=8, padx=20, fill="x")
            self.account_buttons[acc] = btn

        self.spacer = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.spacer.pack(fill="both", expand=True)

        self.bulk_btn = ctk.CTkButton(self.sidebar, text="MODE BULK AUTO (IA)", height=50, 
                                      font=("Segoe UI", 14, "bold"), fg_color="#8E24AA", hover_color="#6A1B9A",
                                      command=self.start_bulk_wizard)
        self.bulk_btn.pack(side="bottom", pady=(0, 10), padx=20, fill="x")
        
        self.last_run_lbl = ctk.CTkLabel(self.sidebar, text="", font=("Segoe UI", 11, "italic"), text_color="#888888", justify="center")
        self.last_run_lbl.pack(side="bottom", pady=20, padx=10)

        # --- MAIN AREA ---
        self.content_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.content_frame.grid(row=0, column=1, sticky="nsew", padx=30, pady=30)
        
        self.status_bar = ctk.CTkLabel(self, text="Prêt. En attente d'ordres.", font=("Segoe UI", 12), text_color="gray")
        self.status_bar.place(relx=0.98, rely=0.98, anchor="se")

        self.show_welcome()

    def update_status(self, text, color="white"):
        """Met à jour la barre de statut et log dans la console"""
        self.status_bar.configure(text=text, text_color=color)
        log_msg(f"[STATUS] {text}")
        self.update() # Force l'interface à se rafraîchir immédiatement

    def show_welcome(self):
        self.clear_content()
        msg = "VEUILLEZ SÉLECTIONNER UN COMPTE\nOU LANCER LE MODE BULK"
        self.welcome_lbl = ctk.CTkLabel(self.content_frame, text=msg, font=("Segoe UI", 18, "bold"), text_color="#555555")
        self.welcome_lbl.pack(expand=True)

    def clear_content(self):
        for widget in self.content_frame.winfo_children(): widget.destroy()

    def select_account(self, account):
        for name, btn in self.account_buttons.items():
            btn.configure(fg_color="transparent", border_color="#3d3d3d")
        self.selected_account = account
        self.account_buttons[account].configure(fg_color="#FE2C55", border_color="#FE2C55")
        self.update_status(f"Compte actif : {account}", "#FE2C55")
        self.show_account_menu()

    def show_account_menu(self):
        self.clear_content()
        header = ctk.CTkLabel(self.content_frame, text=f"Gestionnaire : {self.selected_account}", 
                             font=("Segoe UI", 26, "bold"), text_color="#FE2C55")
        header.pack(pady=(0, 20), anchor="w")

        self.action_area = ctk.CTkFrame(self.content_frame, fg_color="transparent", height=80)
        self.action_area.pack(side="bottom", fill="x", pady=(10, 30))

        self.main_container = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.main_container.pack(side="top", fill="both", expand=True)

        video_card = ctk.CTkFrame(self.main_container, fg_color="#2b2b2b", corner_radius=12)
        video_card.pack(fill="x", pady=10, anchor="n")
        
        self.video_lbl = ctk.CTkLabel(video_card, text="Aucune vidéo sélectionnée", font=("Segoe UI", 13, "italic"))
        self.video_lbl.pack(side="left", padx=20, pady=20)
        
        pick_btn = ctk.CTkButton(video_card, text="CHOISIR UNE VIDÉO", fg_color="#FE2C55", 
                                 command=self.pick_video, font=("Segoe UI", 12, "bold"))
        pick_btn.pack(side="right", padx=20, pady=20)

        self.form_container = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.form_container.pack(fill="both", expand=True)

        if self.video_path:
            self.video_lbl.configure(text=os.path.basename(self.video_path), font=("Segoe UI", 13, "bold"), text_color="white")
            self.build_form()

    def pick_video(self):
        path = filedialog.askopenfilename(title="Sélectionner le clip MP4", filetypes=[("MP4", "*.mp4")])
        if path:
            self.video_path = path
            self.video_lbl.configure(text=os.path.basename(path), font=("Segoe UI", 13, "bold"), text_color="white")
            self.update_status(f"Vidéo chargée : {os.path.basename(path)}")
            self.build_form()

    def build_form(self):
        for widget in self.form_container.winfo_children(): widget.destroy()
        for widget in self.action_area.winfo_children(): widget.destroy()

        desc_card = ctk.CTkFrame(self.form_container, fg_color="#2b2b2b", corner_radius=12)
        desc_card.pack(fill="x", pady=10)
        
        header_frame = ctk.CTkFrame(desc_card, fg_color="transparent")
        header_frame.pack(fill="x", padx=20, pady=(15, 5))
        ctk.CTkLabel(header_frame, text="Légende / Description", font=("Segoe UI", 14, "bold")).pack(side="left")
        
        self.desc_text = ctk.CTkTextbox(desc_card, height=80, fg_color="#1e1e1e")
        self.desc_text.pack(fill="x", padx=20, pady=(0, 15))
        self.desc_text.insert("0.0", "#fyp #viral")

        opt_card = ctk.CTkFrame(self.form_container, fg_color="#2b2b2b", corner_radius=12)
        opt_card.pack(fill="x", pady=10)
        self.auto_pub_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(opt_card, text="Validation Automatique", variable=self.auto_pub_var, progress_color="#FE2C55").pack(side="left", padx=20, pady=20)
        self.prog_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(opt_card, text="Programmer", variable=self.prog_var, command=self.toggle_prog, progress_color="#FE2C55").pack(side="left", padx=20, pady=20)

        self.dt_card = ctk.CTkFrame(self.form_container, fg_color="#2b2b2b", corner_radius=12)
        self.setup_datetime_ui()

        self.launch_btn = ctk.CTkButton(self.action_area, text="LANCER L'UPLOAD", height=55, font=("Segoe UI", 16, "bold"),
                                       fg_color="#FE2C55", hover_color="#c22343", command=self.start_upload)
        self.launch_btn.pack(side="left", fill="x", expand=True, padx=(0, 10))
        
        self.move_btn = ctk.CTkButton(self.action_area, text="ARCHIVER (BOUGER)", height=55, font=("Segoe UI", 16, "bold"),
                                      fg_color="#4CAF50", hover_color="#388E3C", command=self.move_video)
        self.move_btn.pack(side="right", fill="x", expand=True)

    def setup_datetime_ui(self):
        now = datetime.now()
        inner = ctk.CTkFrame(self.dt_card, fg_color="transparent")
        inner.pack(pady=15, padx=20)
        
        day_f = ctk.CTkFrame(inner, fg_color="transparent")
        day_f.grid(row=0, column=0, padx=5)
        ctk.CTkButton(day_f, text="-", width=30, command=self.decrement_day).pack(side="left", padx=2)
        self.dd = ctk.CTkOptionMenu(day_f, values=[], width=70, command=self.update_optimal)
        self.dd.pack(side="left")
        ctk.CTkButton(day_f, text="+", width=30, command=self.increment_day).pack(side="left", padx=2)
        
        self.mm = ctk.CTkOptionMenu(inner, values=[str(i).zfill(2) for i in range(1, 13)], width=75, command=self.update_days_and_optimal)
        self.mm.set(str(now.month).zfill(2)); self.mm.grid(row=0, column=1, padx=5)
        self.yyyy = ctk.CTkOptionMenu(inner, values=[str(now.year), str(now.year+1)], width=90, command=self.update_days_and_optimal)
        self.yyyy.set(str(now.year)); self.yyyy.grid(row=0, column=2, padx=5)
        self.hh = ctk.CTkOptionMenu(inner, values=[str(i).zfill(2) for i in range(24)], width=75); self.hh.set("18"); self.hh.grid(row=0, column=3, padx=(30, 5))
        self.min = ctk.CTkOptionMenu(inner, values=[str(i).zfill(2) for i in range(0, 60, 5)], width=75); self.min.set("00"); self.min.grid(row=0, column=4, padx=5)
       
        self.optimal_frame = ctk.CTkFrame(self.dt_card, fg_color="transparent")
        self.optimal_frame.pack(pady=10, fill="x")
        self.update_days()
        self.dd.set(str(now.day).zfill(2))
        self.update_optimal()

    def toggle_prog(self):
        if self.prog_var.get(): self.dt_card.pack(fill="x", pady=10)
        else: self.dt_card.pack_forget()

    def update_days_and_optimal(self, _=None):
        self.update_days(); self.update_optimal()

    def update_days(self, _=None):
        y, m = int(self.yyyy.get()), int(self.mm.get())
        days = [str(i).zfill(2) for i in range(1, calendar.monthrange(y, m)[1] + 1)]
        self.dd.configure(values=days)

    def update_optimal(self, _=None):
        weekday = date(int(self.yyyy.get()), int(self.mm.get()), int(self.dd.get())).weekday()
        times = OPTIMAL_TIMES.get(weekday, ["18:00"])
        for w in self.optimal_frame.winfo_children(): w.destroy()
        for t in times:
            hh, mm = t.split(":")
            ctk.CTkButton(self.optimal_frame, text=t, width=60, height=28, 
                          command=lambda h=hh, m=mm: self.set_time(h, m)).pack(side="left", padx=5)

    def set_time(self, h, m):
        self.hh.set(h); self.min.set(m)

    def increment_day(self):
        c = datetime(int(self.yyyy.get()), int(self.mm.get()), int(self.dd.get())) + timedelta(days=1)
        self.yyyy.set(str(c.year)); self.mm.set(str(c.month).zfill(2)); self.update_days(); self.dd.set(str(c.day).zfill(2)); self.update_optimal()

    def decrement_day(self):
        c = datetime(int(self.yyyy.get()), int(self.mm.get()), int(self.dd.get())) - timedelta(days=1)
        self.yyyy.set(str(c.year)); self.mm.set(str(c.month).zfill(2)); self.update_days(); self.dd.set(str(c.day).zfill(2)); self.update_optimal()

    def move_video(self):
        if not self.video_path: return
        self._move_single_file(self.video_path)
        self.video_path = ""
        self.show_account_menu()

    def _move_single_file(self, path):
        dest = r"E:\Tiktok\Vids\Sent"
        os.makedirs(dest, exist_ok=True)
        try:
            shutil.move(path, os.path.join(dest, os.path.basename(path)))
            self.update_status(f"Fichier archivé : {os.path.basename(path)}", "green")
        except Exception as e:
            self.update_status(f"Erreur archivage : {e}", "red")

    def start_upload(self):
        if not self.video_path: return
        
        if self.prog_var.get():
            sched_str = f"{self.dd.get()}/{self.mm.get()}/{self.yyyy.get()}\nà {self.hh.get()}:{self.min.get()}"
            self.last_run_lbl.configure(text=f"Dernière prog :\n{sched_str}")
        else:
            now_str = datetime.now().strftime("%d/%m/%Y\nà %H:%M")
            self.last_run_lbl.configure(text=f"Dernier envoi :\n{now_str}")

        data = {
            "profile": ACCOUNTS[self.selected_account], "video": self.video_path,
            "desc": self.desc_text.get("0.0", "end").strip(), "is_sched": self.prog_var.get(),
            "auto_pub": self.auto_pub_var.get(), "day": self.dd.get().lstrip('0'),
            "month_target": int(self.mm.get()), "hour": self.hh.get(), "minute": self.min.get(),
            "yyyy": self.yyyy.get()
        }
        # Threading pour ne pas bloquer l'interface pendant l'upload manuel
        threading.Thread(target=self.run_bot, args=(data,), daemon=True).start()

    # --- BULK & IA LOGIC ---
    def start_bulk_wizard(self):
        self.bulk_window = ctk.CTkToplevel(self)
        self.bulk_window.title("Sélecteur de Comptes - BULK")
        self.bulk_window.geometry("500x600")
        self.bulk_window.attributes("-topmost", True)
        
        lbl = ctk.CTkLabel(self.bulk_window, text="Quels comptes cibler ?", font=("Segoe UI", 18, "bold"))
        lbl.pack(pady=20)
        
        self.bulk_account_vars = {}
        for acc in ACCOUNTS.keys():
            frame = ctk.CTkFrame(self.bulk_window, fg_color="transparent")
            frame.pack(fill="x", padx=50, pady=5)
            
            btn = ctk.CTkButton(frame, text=acc, fg_color="#333333", 
                                command=lambda a=acc: self.toggle_bulk_acc(a))
            btn.pack(fill="x", ipady=5)
            self.bulk_account_vars[acc] = {"selected": False, "btn": btn}

        confirm_btn = ctk.CTkButton(self.bulk_window, text="VALIDER ET CONTINUER ->", 
                                   fg_color="#4CAF50", hover_color="#388E3C", height=45,
                                   command=self.process_bulk_videos_selection)
        confirm_btn.pack(side="bottom", pady=20, padx=50, fill="x")

    def toggle_bulk_acc(self, acc):
        state = self.bulk_account_vars[acc]
        state["selected"] = not state["selected"]
        color = "#4CAF50" if state["selected"] else "#333333"
        state["btn"].configure(fg_color=color)

    def process_bulk_videos_selection(self):
        selected_accounts = [acc for acc, data in self.bulk_account_vars.items() if data["selected"]]
        if not selected_accounts: return
        
        self.bulk_window.destroy()
        self.bulk_queue = [] 

        for acc in selected_accounts:
            filepaths = filedialog.askopenfilenames(
                title=f"Vidéos pour : {acc}", 
                filetypes=[("MP4", "*.mp4")]
            )
            if not filepaths: continue

            for path in filepaths:
                video_conf = self.ask_time_for_video(acc, path)
                if video_conf:
                    self.bulk_queue.append(video_conf)
        
        if self.bulk_queue:
            self.run_bulk_processing()

    def ask_time_for_video(self, account, filepath):
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Horaire : {os.path.basename(filepath)}")
        dialog.geometry("400x450")
        dialog.attributes("-topmost", True)
        
        result = {"account": account, "video": filepath, "is_sched": False}
        
        ctk.CTkLabel(dialog, text=f"Compte: {account}", text_color="gray").pack()
        ctk.CTkLabel(dialog, text=os.path.basename(filepath), font=("Arial", 11, "bold")).pack(pady=5)
        
        def set_now():
            result["is_sched"] = False
            dialog.destroy()
            
        def set_sched(time_str):
            result["is_sched"] = True
            h, m = time_str.split(":")
            
            raw_date = date_menu.get()
            d_day, d_month, d_year = raw_date.split("/")
            
            result["day"] = str(int(d_day))
            result["month_target"] = int(d_month)
            result["yyyy"] = str(d_year)
            result["hour"] = h
            result["minute"] = m
            dialog.destroy()

        ctk.CTkButton(dialog, text="POSTER MAINTENANT (Direct)", fg_color="#FE2C55", command=set_now).pack(pady=10, fill="x", padx=40)
        
        ctk.CTkLabel(dialog, text="--- OU PROGRAMMER ---").pack(pady=5)
        
        dates_list = []
        now = datetime.now()
        for i in range(8):
            future_d = now + timedelta(days=i)
            dates_list.append(future_d.strftime("%d/%m/%Y"))
            
        date_menu = ctk.CTkOptionMenu(dialog, values=dates_list)
        date_menu.pack(pady=5)
        date_menu.set(dates_list[1])
        
        viral_hours = ["07:00", "11:00", "16:00", "18:00", "20:00"]
        grid_f = ctk.CTkFrame(dialog, fg_color="transparent")
        grid_f.pack(pady=10)
        
        for i, t in enumerate(viral_hours):
            ctk.CTkButton(grid_f, text=t, width=60, command=lambda x=t: set_sched(x)).grid(row=0, column=i, padx=5)

        dialog.wait_window()
        return result

    def remove_emojis(self, text):
        return re.sub(r'[^\w\s,.\'#?!éèàêâûôîçÉÈÀÊÂÛÔÎÇ\-]', '', text).strip()

    def run_bulk_processing(self):
        threading.Thread(target=self._process_queue_thread, daemon=True).start()

    def _process_queue_thread(self):
        total = len(self.bulk_queue)
        
        # LOG WHISPER
        if not self.whisper_model:
            self.update_status("Chargement du modèle Whisper (Audio)...", "orange")
            try:
                start_w = time.time()
                self.whisper_model = whisper.load_model("base")
                log_msg(f"[WHISPER] Modèle chargé en {round(time.time() - start_w, 2)}s")
            except Exception as e:
                self.update_status(f"Erreur Whisper : {e}", "red")
                print(e); return

        for idx, item in enumerate(self.bulk_queue):
            try:
                filename = os.path.basename(item['video'])
                self.update_status(f"[{idx+1}/{total}] Traitement : {filename}", "#8E24AA")
                
                # 1. TRANSCRIPTION
                self.update_status(f"[{idx+1}/{total}] Transcription audio en cours...", "#8E24AA")
                transcription = self.whisper_model.transcribe(item["video"])["text"]
                log_msg(f"[TRANSCRIPTION] Résultat : {transcription[:100]}...") # Affiche les 100 premiers caractères
                
                # 2. IA GENERATION
                account_name = item["account"]
                self.update_status(f"[{idx+1}/{total}] Envoi Prompt à Gemini...", "#8E24AA")
                
                final_prompt = GEMINI_PROMPT_BASE.format(
                    account=account_name,
                    filename=filename, 
                    transcription=transcription
                )
                
                try:
                    generated_text = self.gemini_manager.get_content(final_prompt)
                    log_msg("--- REPONSE RAW GEMINI ---")
                    print(generated_text)
                    log_msg("--------------------------")
                except Exception as e:
                    log_msg(f"[ERREUR IA] {e}")
                    generated_text = f"Video {idx} #viral #fyp" 

                # 3. NETTOYAGE
                self.update_status(f"[{idx+1}/{total}] Nettoyage du texte (No Emoji)...", "#8E24AA")
                clean_text = self.remove_emojis(generated_text)
                log_msg(f"[CLEAN TEXT] {clean_text}")
                
                item["desc"] = clean_text
                item["profile"] = ACCOUNTS[item["account"]]
                item["auto_pub"] = True 

                # 4. UPLOAD
                self.update_status(f"[{idx+1}/{total}] Lancement Navigateur...", "#8E24AA")
                self.run_bot(item)
                
                # 5. ARCHIVAGE
                self.update_status(f"[{idx+1}/{total}] Archivage fichier...", "#8E24AA")
                self._move_single_file(item["video"])
                
                self.update_status(f"[{idx+1}/{total}] Pause 5s avant suite...", "orange")
                time.sleep(5) 

            except Exception as e:
                log_msg(f"[CRITICAL ERROR] Sur fichier {item['video']}: {e}")
                self.update_status(f"Erreur critique sur {os.path.basename(item['video'])}", "red")
                continue

        self.update_status("TERMINE ! Tous les fichiers de la liste ont été traités.", "green")

    def run_bot(self, data):
        self.p = None
        ctx = None
        try:
            log_msg("[PLAYWRIGHT] Démarrage moteur...")
            self.p = sync_playwright().start()
            
            log_msg(f"[PLAYWRIGHT] Ouverture profil Chrome : {data['profile']}")
            ctx = self.p.chromium.launch_persistent_context(user_data_dir=CHROME_DATA, channel="chrome", headless=False,
                args=[f"--profile-directory={data['profile']}", "--disable-blink-features=AutomationControlled"], no_viewport=True)
            page = ctx.new_page()
            
            # Anti-détection basique
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            log_msg("[NAV] Aller vers Tiktok Upload")
            page.goto("https://www.tiktok.com/tiktokstudio/upload")
            
            log_msg(f"[UPLOAD] Envoi du fichier : {data['video']}")
            page.set_input_files('input[type="file"]', data["video"])
            
            log_msg("[WAIT] Pause 10s pour chargement initial...")
            time.sleep(10)
            
            log_msg("[EDIT] Modification Description")
            desc = page.locator('div[contenteditable="true"]')
            desc.click()
            page.keyboard.press('Control+A')
            page.keyboard.press('Backspace')
            
            log_msg("[EDIT] Écriture description IA")
            page.keyboard.type(data["desc"])
            
            if data["is_sched"]:
                log_msg("[SCHED] Mode programmé activé")
                page.locator('text="Programmer"').first.click()
                time.sleep(1) 

                year = int(data["yyyy"]) if "yyyy" in data else datetime.now().year
                month = int(data["month_target"])
                day = int(data["day"])
                
                log_msg(f"[SCHED] Date cible : {day}/{month}/{year}")
                
                # Ouverture calendrier
                date_box = page.locator('.TUXInputBox').nth(1)
                date_box.click()
                time.sleep(1)

                # Navigation calendrier
                while True:
                    month_title = page.locator('span.month-title').inner_text().strip().lower()
                    year_title = page.locator('span.year-title').inner_text().strip()
                    mois_fr = ['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre']
                    mois_num = mois_fr.index(month_title) + 1 if month_title in mois_fr else 0
                    
                    if int(year_title) == year and mois_num == month:
                        break
                    elif int(year_title) < year or (int(year_title) == year and mois_num < month):
                        page.locator('span.arrow').nth(1).click()
                        time.sleep(0.5)
                    else:
                        page.locator('span.arrow').nth(0).click()
                        time.sleep(0.5)
                
                # Selection jour
                page.locator(f'span.day.valid:text-is("{day}")').first.click()
                time.sleep(1)
                
                # Selection heure
                log_msg(f"[SCHED] Heure cible : {data['hour']}:{data['minute']}")
                time_input = page.locator('input.TUXTextInputCore-input[type="text"][value*=":"]')
                time_input.click()
                time.sleep(0.5)
                
                page.locator(f'span.tiktok-timepicker-option-text.tiktok-timepicker-left:text-is("{data["hour"]}")').click()
                time.sleep(0.5)
                
                page.locator(f'span.tiktok-timepicker-option-text.tiktok-timepicker-right:text-is("{data["minute"]}")').click()
                time.sleep(0.5)
                
                # Click dehors pour valider
                page.mouse.click(0, 0)
                time.sleep(1)

            # Verification Checkbox
            try:
                verif_label = page.locator('span.headline', has_text='Vérification de contenu simple')
                if verif_label.count() > 0:
                    switch_input = verif_label.locator('xpath=../../..').locator('input[type="checkbox"]')
                    if switch_input.count() > 0:
                        if switch_input.is_checked():
                            log_msg("[CHECK] Désactivation 'Vérification de contenu'")
                            switch_input.click()
            except: pass

            if data["auto_pub"]:
                log_msg("[PUBLISH] Clic sur le bouton final...")
                btn = page.locator('button:has-text("Publier"), button:has-text("Programmer")').last
                time.sleep(2)
                btn.click()
                log_msg("[SUCCESS] Upload confirmé par clic.")
                self.update_status(f"Upload OK : {os.path.basename(data['video'])}", "green")
                time.sleep(5)
            else:
                log_msg("[MANUAL] En attente de validation manuelle")
                self.update_status("Succès upload ! (à valider manuellement)", "green")

        except Exception as e:
            log_msg(f"[ERROR BOT] {e}")
            self.update_status(f"Erreur Upload : {e}", "red")
        finally:
            if data.get("auto_pub"):
                log_msg("[CLEANUP] Fermeture navigateur")
                if ctx is not None:
                    try: ctx.close()
                    except: pass
                if self.p is not None:
                    try: self.p.stop()
                    except: pass

if __name__ == "__main__":
    app = TikTokUploaderUI()
    app.mainloop()