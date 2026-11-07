import ctypes
import json
import os
import queue
import subprocess
import threading
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------- settings
BASE_URL = "https://tokenconductor.agentic.accenturefederaldev.com/v1/messages"
MODEL = "claude-sonnet-5-5"
API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # set in PowerShell, never saved here

CHAT_PROMPT = (
    "You are a helpful voice assistant for company employees. "
    "Your answers will be read aloud, so use short, natural, spoken-style "
    "sentences. Never use bullet points, markdown, or special formatting."
)

COACH_PROMPT = (
    "You are a calm voice coach. The user wants to complete a task while "
    "listening to you. Break the task into clear, small steps, each one or two "
    "short spoken-style sentences. Reply with ONLY a JSON list of strings, "
    "one string per step, with no other text."
)

SIMPLIFY_PROMPT = (
    "Rewrite the text below in plain, simple language that is easy to follow "
    "when heard aloud. Keep every important fact and any deadlines or actions. "
    "Use short sentences, no jargon, and no bullet points or markdown."
)

# colours and fonts
BG = "#F4F6FA"
HEADER = "#1F3A5F"
ACCENT = "#2D6CDF"
ACCENT_DARK = "#2457B5"
SOFT = "#E3E8F2"
SOFT_DARK = "#D3DAE8"
TEXT = "#1B2430"
MUTED = "#6B7A90"
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------- state
state = {"voice": True, "rate": 0, "voice_name": ""}
history = []
ui_queue = queue.Queue()
coach = {"active": False, "task": "", "steps": [], "idx": 0}
speech_proc = None
speech_token = 0


def post(kind, payload=""):
    """Background threads send messages to the window through this queue."""
    ui_queue.put((kind, payload))


# ---------------------------------------------------------------- speech
def speak_blocking(text):
    """Read text aloud with the voice built into Windows. Returns when done."""
    global speech_proc
    if not state["voice"] or not text.strip():
        return
    token = speech_token
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = [int]$env:SPEAK_RATE; "
        "if ($env:SPEAK_VOICE) { try { $s.SelectVoice($env:SPEAK_VOICE) } catch {} }; "
        "$s.Speak($env:SPEAK_TEXT)"
    )
    env = dict(
        os.environ,
        SPEAK_TEXT=text,
        SPEAK_RATE=str(state["rate"]),
        SPEAK_VOICE=state["voice_name"],
    )
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            env=env,
            creationflags=NO_WINDOW,
        )
    except Exception as e:
        post("system", "Could not play audio: " + str(e))
        return
    speech_proc = proc
    if token != speech_token:  # stopped while starting up
        stop_speaking()
        return
    proc.wait()


def stop_speaking():
    global speech_token
    speech_token += 1
    proc = speech_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def load_voices():
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
        ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
        names = [n.strip() for n in result.stdout.splitlines() if n.strip()]
    except Exception:
        names = []
    post("voices", names)


# ---------------------------------------------------------------- Claude
def call_gateway(messages, system, max_tokens, auth_style):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if auth_style == "x-api-key":
        headers["x-api-key"] = API_KEY
    else:
        headers["Authorization"] = "Bearer " + API_KEY

    req = urllib.request.Request(BASE_URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["content"][0]["text"]


def ask_claude(messages, system=CHAT_PROMPT, max_tokens=500):
    for style in ("x-api-key", "bearer"):
        try:
            return call_gateway(messages, system, max_tokens, style)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code == 401 and style == "x-api-key":
                continue
            post("system", "Gateway error " + str(e.code) + ": " + detail)
            return None
        except urllib.error.URLError as e:
            post("system", "Could not reach the gateway: " + str(e.reason))
            return None
        except Exception as e:
            post("system", "Something went wrong: " + str(e))
            return None
    post("system", "The gateway rejected the key (401). Check your key.")
    return None


def parse_steps(raw):
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        steps = json.loads(cleaned)
        if isinstance(steps, list):
            return [str(s) for s in steps]
    except ValueError:
        pass
    return [line.strip() for line in raw.splitlines() if line.strip()]


def chunk_text(text, limit=1500):
    chunks, current = [], ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = (current + " " + line).strip()
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------- workers
def run_bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def speak_with_status(text):
    if state["voice"]:
        post("status", "Speaking...  (Esc or Stop to quiet)")
        speak_blocking(text)
    post("status", "Ready")


def chat_worker(text):
    history.append({"role": "user", "content": text})
    post("status", "Thinking...")
    reply = ask_claude(history)
    if reply is None:
        history.pop()
        post("status", "Ready")
        return
    history.append({"role": "assistant", "content": reply})
    post("claude", reply)
    speak_with_status(reply)


def question_worker(question):
    post("status", "Thinking...")
    reply = ask_claude([{"role": "user", "content": question}])
    if reply is None:
        post("status", "Ready")
        return
    post("claude", reply)
    speak_with_status(reply)


def coach_fetch_worker(task):
    post("status", "Planning the steps...")
    raw = ask_claude(
        [{"role": "user", "content": "Give me the steps to: " + task}],
        COACH_PROMPT,
        1200,
    )
    if raw is None:
        post("status", "Ready")
        return
    steps = parse_steps(raw)
    if not steps:
        post("system", "Claude didn't return any steps. Try rewording the task.")
        post("status", "Ready")
        return
    post("coach_steps", (task, steps))


def read_worker(text):
    token = speech_token
    post("system", "Reading aloud. Press Stop or Esc to end.")
    for chunk in chunk_text(text):
        if token != speech_token:
            break
        post("doc", chunk)
        if state["voice"]:
            post("status", "Reading...")
            speak_blocking(chunk)
    post("status", "Ready")


def simplify_worker(text):
    post("status", "Simplifying...")
    result = ask_claude([{"role": "user", "content": text}], SIMPLIFY_PROMPT, 1000)
    if result is None:
        post("status", "Ready")
        return
    post("claude", result)
    speak_with_status(result)


# ---------------------------------------------------------------- window
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on Windows
except Exception:
    pass

root = tk.Tk()

if not API_KEY:
    root.withdraw()
    messagebox.showerror(
        "No key found",
        'Close this window, then in PowerShell run:\n\n'
        '$env:ANTHROPIC_API_KEY="your-key"\n\n'
        "and start the helper again from that same window.",
    )
    raise SystemExit

root.title("Voice Helper")
root.configure(bg=BG)
root.minsize(340, 480)

W, H = 380, 600
x = root.winfo_screenwidth() - W - 20
y = max(root.winfo_screenheight() - H - 70, 0)
root.geometry("%dx%d+%d+%d" % (W, H, x, y))
root.attributes("-topmost", True)

status_var = tk.StringVar(value="Ready")
voice_var = tk.BooleanVar(value=True)
pin_var = tk.BooleanVar(value=True)


def make_button(parent, text, command, primary=False):
    base = ACCENT if primary else SOFT
    hover = ACCENT_DARK if primary else SOFT_DARK
    btn = tk.Button(
        parent, text=text, command=command, relief="flat", bd=0,
        padx=10, pady=6, font=FONT_BOLD, cursor="hand2",
        bg=base, fg="white" if primary else TEXT,
        activebackground=hover, activeforeground="white" if primary else TEXT,
    )
    btn.bind("<Enter>", lambda e: btn.configure(bg=hover))
    btn.bind("<Leave>", lambda e: btn.configure(bg=base))
    return btn


# header
header = tk.Frame(root, bg=HEADER)
header.pack(side="top", fill="x")
tk.Label(header, text="Voice Helper", bg=HEADER, fg="white",
         font=("Segoe UI", 12, "bold")).pack(side="left", padx=12, pady=10)


def toggle_pin():
    root.attributes("-topmost", pin_var.get())


tk.Checkbutton(
    header, text="Keep on top", variable=pin_var, command=toggle_pin,
    bg=HEADER, fg="white", selectcolor=HEADER, activebackground=HEADER,
    activeforeground="white", font=FONT_SMALL, bd=0, highlightthickness=0,
).pack(side="right", padx=10)

# settings row (bottom)
settings_frame = tk.Frame(root, bg=BG)
settings_frame.pack(side="bottom", fill="x", padx=12, pady=(0, 10))


def on_voice_toggle():
    state["voice"] = voice_var.get()
    if not state["voice"]:
        stop_speaking()


tk.Checkbutton(
    settings_frame, text="Voice", variable=voice_var, command=on_voice_toggle,
    bg=BG, fg=TEXT, selectcolor="white", activebackground=BG, font=FONT_SMALL,
    bd=0, highlightthickness=0,
).pack(side="left")

tk.Label(settings_frame, text="Speed", bg=BG, fg=MUTED,
         font=FONT_SMALL).pack(side="left", padx=(8, 2))


def on_speed(value):
    state["rate"] = int(round(float(value)))


speed = ttk.Scale(settings_frame, from_=-10, to=10, orient="horizontal",
                  length=90, command=on_speed)
speed.set(0)
speed.pack(side="left")

voice_box = ttk.Combobox(settings_frame, state="readonly", width=14, font=FONT_SMALL)
voice_box.pack(side="right")
voice_box.set("Default voice")


def on_voice_pick(event=None):
    state["voice_name"] = voice_box.get()


voice_box.bind("<<ComboboxSelected>>", on_voice_pick)

# action buttons (bottom)
actions = tk.Frame(root, bg=BG)
actions.pack(side="bottom", fill="x", padx=12, pady=(0, 8))

# input row (bottom)
input_frame = tk.Frame(root, bg=BG)
input_frame.pack(side="bottom", fill="x", padx=12, pady=(0, 8))

entry = tk.Entry(input_frame, font=FONT, relief="flat", bg="white", fg=TEXT,
                 highlightthickness=1, highlightbackground=SOFT_DARK,
                 highlightcolor=ACCENT, insertbackground=TEXT)
entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 6))

# status line (bottom)
status_label = tk.Label(root, textvariable=status_var, bg=BG, fg=MUTED,
                        font=FONT_SMALL, anchor="w")
status_label.pack(side="bottom", fill="x", padx=14, pady=(0, 4))

# coach bar (hidden until coach mode starts)
coach_bar = tk.Frame(root, bg="#E8F0FE")
coach_label = tk.Label(coach_bar, text="", bg="#E8F0FE", fg=HEADER, font=FONT_BOLD)
coach_label.pack(side="left", padx=10, pady=6)

# chat area
chat_frame = tk.Frame(root, bg=BG)
chat_frame.pack(side="top", fill="both", expand=True, padx=12, pady=10)
scroll = tk.Scrollbar(chat_frame)
scroll.pack(side="right", fill="y")
chat = tk.Text(
    chat_frame, wrap="word", state="disabled", font=FONT, bg="white", fg=TEXT,
    relief="flat", padx=10, pady=8, yscrollcommand=scroll.set,
    highlightthickness=1, highlightbackground=SOFT_DARK, cursor="arrow",
)
chat.pack(side="left", fill="both", expand=True)
scroll.config(command=chat.yview)

chat.tag_config("user_label", foreground=ACCENT, font=FONT_BOLD)
chat.tag_config("claude_label", foreground=HEADER, font=FONT_BOLD)
chat.tag_config("doc_label", foreground=MUTED, font=FONT_BOLD)
chat.tag_config("user", foreground=TEXT, font=FONT)
chat.tag_config("claude", foreground=TEXT, font=FONT)
chat.tag_config("doc", foreground="#333333", font=FONT)
chat.tag_config("system", foreground=MUTED, font=("Segoe UI", 9, "italic"))

LABELS = {"user": "You", "claude": "Assistant", "doc": "Reading"}


def add_message(kind, text):
    chat.configure(state="normal")
    if kind in LABELS:
        chat.insert("end", LABELS[kind] + "\n", (kind + "_label",))
    chat.insert("end", text + "\n\n", (kind,))
    chat.configure(state="disabled")
    chat.see("end")


# ---------------------------------------------------------------- coach logic
def show_coach_bar():
    coach_bar.pack(side="bottom", fill="x", padx=12, pady=(0, 6), before=status_label)


def end_coach():
    coach["active"] = False
    coach_bar.pack_forget()


def present_step():
    stop_speaking()
    n = len(coach["steps"])
    i = coach["idx"]
    text = "Step %d of %d. %s" % (i + 1, n, coach["steps"][i])
    coach_label.config(text="Step %d of %d" % (i + 1, n))
    add_message("claude", text)
    run_bg(speak_with_status, text)


def coach_next():
    if not coach["active"]:
        return
    if coach["idx"] + 1 < len(coach["steps"]):
        coach["idx"] += 1
        present_step()
    else:
        stop_speaking()
        done = "That's the last step. You're done."
        add_message("claude", done)
        run_bg(speak_with_status, done)
        end_coach()


def coach_repeat():
    if coach["active"]:
        present_step()


def coach_end():
    stop_speaking()
    end_coach()
    add_message("system", "Coach ended.")
    status_var.set("Ready")


make_button(coach_bar, "Next", coach_next, primary=True).pack(side="right", padx=(4, 8), pady=4)
make_button(coach_bar, "Repeat", coach_repeat).pack(side="right", padx=4, pady=4)
make_button(coach_bar, "End", coach_end).pack(side="right", padx=4, pady=4)


def start_coach(task, steps):
    coach.update(active=True, task=task, steps=steps, idx=0)
    show_coach_bar()
    add_message("system", "Press Next when you finish each step, or type a question about the current step.")
    present_step()


# ---------------------------------------------------------------- user actions
def on_send(event=None):
    text = entry.get().strip()
    if not text:
        return
    entry.delete(0, "end")
    stop_speaking()
    add_message("user", text)
    status_var.set("Thinking...")
    if coach["active"]:
        step = coach["steps"][coach["idx"]]
        question = ("I'm doing this task: %s. I'm on this step: %s My question: %s"
                    % (coach["task"], step, text))
        run_bg(question_worker, question)
    else:
        run_bg(chat_worker, text)


def on_coach():
    task = entry.get().strip()
    if not task:
        add_message("system", "Type a task in the box first (for example: set up a Jira board), then press Coach.")
        return
    entry.delete(0, "end")
    stop_speaking()
    if coach["active"]:
        end_coach()
    add_message("user", "Coach me through: " + task)
    run_bg(coach_fetch_worker, task)


def start_doc(action, text, label):
    stop_speaking()
    if action == "read":
        add_message("system", "Reading " + label + ".")
        run_bg(read_worker, text[:50000])
    else:
        add_message("system", "Simplifying " + label + ".")
        status_var.set("Simplifying...")
        run_bg(simplify_worker, text[:20000])


def doc_from_file(action):
    path = filedialog.askopenfilename(
        title="Choose a text file",
        filetypes=[("Text files", "*.txt *.md *.csv"), ("All files", "*.*")],
    )
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        add_message("system", "Couldn't open that file: " + str(e))
        return
    if not text.strip():
        add_message("system", "That file is empty.")
        return
    start_doc(action, text, os.path.basename(path))


def doc_from_clipboard(action):
    try:
        text = root.clipboard_get()
    except tk.TclError:
        text = ""
    if not text.strip():
        add_message("system", "The clipboard has no text. Copy some text first.")
        return
    start_doc(action, text, "the copied text")


def source_menu(action, widget):
    menu = tk.Menu(root, tearoff=0, font=FONT)
    menu.add_command(label="From a text file...", command=lambda: doc_from_file(action))
    menu.add_command(label="From copied text (clipboard)", command=lambda: doc_from_clipboard(action))
    try:
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
    finally:
        menu.grab_release()


def on_stop(event=None):
    stop_speaking()
    status_var.set("Ready")


send_btn = make_button(input_frame, "Send", on_send, primary=True)
send_btn.pack(side="left")
entry.bind("<Return>", on_send)

coach_btn = make_button(actions, "Coach", on_coach, primary=True)
coach_btn.pack(side="left", padx=(0, 6))
read_btn = make_button(actions, "Read aloud", lambda: source_menu("read", read_btn))
read_btn.pack(side="left", padx=(0, 6))
simplify_btn = make_button(actions, "Simplify", lambda: source_menu("simplify", simplify_btn))
simplify_btn.pack(side="left", padx=(0, 6))
make_button(actions, "Stop", on_stop).pack(side="right")

root.bind("<Escape>", on_stop)


# ---------------------------------------------------------------- queue pump
def handle(kind, payload):
    if kind in ("claude", "doc", "system", "user"):
        add_message(kind, payload)
    elif kind == "status":
        status_var.set(payload)
    elif kind == "voices":
        if payload:
            voice_box["values"] = payload
    elif kind == "coach_steps":
        task, steps = payload
        start_coach(task, steps)


def pump():
    try:
        while True:
            kind, payload = ui_queue.get_nowait()
            handle(kind, payload)
    except queue.Empty:
        pass
    root.after(100, pump)


def on_close():
    stop_speaking()
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)

add_message(
    "system",
    "Hi! Type a question and press Enter, or type a task and press Coach for "
    "step-by-step help. Use Read aloud to hear a document, or Simplify to hear "
    "it in plain language. Press Esc to quiet the voice.",
)
run_bg(load_voices)
root.after(100, pump)
entry.focus_set()
root.mainloop()
