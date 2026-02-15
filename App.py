# app.py
# Movie Shelf — Local/Private + Optional OMDb + Per-browser user isolation (no accounts)
# Runs on localhost AND Streamlit Cloud.
#
# Key features:
# - Pure black UI
# - Local-first SQLite
# - Posters cached locally (resized + compressed to keep storage low)
# - Lists (create + add + reorder)
# - Optional OMDb lookup (API key via st.secrets or env var)
# - Option A isolation: anonymous per-browser user_id stored in browser localStorage
# - Export/Import backup as a zip

import os
import io
import uuid
import shutil
import zipfile
import sqlite3
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Tuple
from io import BytesIO

import requests
import streamlit as st
from PIL import Image
from streamlit_local_storage import LocalStorage

APP_TITLE = "Movie Shelf"

# ---------------------------
# Secrets / Env
# ---------------------------
# Works both locally and on Streamlit Cloud.
OMDB_API_KEY = (st.secrets.get("OMDB_API_KEY", "") or os.getenv("OMDB_API_KEY", "")).strip()

# ---------------------------
# Per-browser user isolation (Option A)
# ---------------------------
# Each browser gets a random UUID stored in localStorage.
# The app stores data in data/users/<user_id>/movies.db and posters/
localS = LocalStorage()
user_id = localS.getItem("movie_shelf_user_id")
if not user_id:
    user_id = str(uuid.uuid4())
    localS.setItem("movie_shelf_user_id", user_id)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.path.join(BASE_DIR, "data", "users", user_id)
os.makedirs(USER_DIR, exist_ok=True)

DB_PATH = os.path.join(USER_DIR, "movies.db")
POSTERS_DIR = os.path.join(USER_DIR, "posters")

# ---------------------------
# Database
# ---------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(POSTERS_DIR, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                year INTEGER,
                plot TEXT,
                poster_url TEXT,
                poster_path TEXT,
                format TEXT DEFAULT 'Blu-ray',
                watched INTEGER DEFAULT 0,
                location TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                source TEXT,
                source_id TEXT
            );

            CREATE TABLE IF NOT EXISTS lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS list_items (
                list_id INTEGER NOT NULL,
                movie_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (list_id, movie_id),
                FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE,
                FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE
            );
            """
        )

# ---------------------------
# Poster caching (semi-low quality)
# ---------------------------
def safe_filename(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return f"{h}.jpg"

def download_poster(url: str) -> Optional[str]:
    """Downloads a poster, resizes it, and compresses it to keep storage low."""
    if not url or url.strip().lower() in {"n/a", "na", "none"}:
        return None

    filename = safe_filename(url)
    path = os.path.join(POSTERS_DIR, filename)
    if os.path.exists(path):
        return path

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()

        img = Image.open(BytesIO(r.content)).convert("RGB")

        # Resize to keep storage low
        target_width = 300  # try 400 if you want slightly sharper posters
        if img.width > target_width:
            ratio = target_width / float(img.width)
            target_height = int(img.height * ratio)
            img = img.resize((target_width, target_height), Image.LANCZOS)

        # Compress JPEG
        os.makedirs(POSTERS_DIR, exist_ok=True)
        img.save(path, format="JPEG", quality=70, optimize=True)

        return path
    except Exception:
        return None

# ---------------------------
# OMDb fetch (optional)
# ---------------------------
@dataclass
class MovieMeta:
    title: str
    year: Optional[int]
    plot: Optional[str]
    poster_url: Optional[str]
    source: str = "omdb"
    source_id: Optional[str] = None

def omdb_search(title: str) -> List[Tuple[str, str, str]]:
    """Returns list of (Title, Year, imdbID)"""
    if not OMDB_API_KEY:
        return []
    try:
        r = requests.get(
            "https://www.omdbapi.com/",
            params={"apikey": OMDB_API_KEY, "s": title},
            timeout=15,
        )
        data = r.json()
        if data.get("Response") != "True":
            return []
        out = []
        for item in data.get("Search", []):
            out.append((item.get("Title", ""), item.get("Year", ""), item.get("imdbID", "")))
        return out
    except Exception:
        return []

def omdb_get(imdb_id: str) -> Optional[MovieMeta]:
    if not OMDB_API_KEY or not imdb_id:
        return None
    try:
        r = requests.get(
            "https://www.omdbapi.com/",
            params={"apikey": OMDB_API_KEY, "i": imdb_id, "plot": "short"},
            timeout=15,
        )
        data = r.json()
        if data.get("Response") != "True":
            return None
        year = None
        try:
            year = int(str(data.get("Year", "")).split("–")[0])
        except Exception:
            year = None
        return MovieMeta(
            title=data.get("Title") or "",
            year=year,
            plot=data.get("Plot"),
            poster_url=data.get("Poster"),
            source_id=data.get("imdbID"),
        )
    except Exception:
        return None

# ---------------------------
# CRUD
# ---------------------------
def add_movie(
    title: str,
    year: Optional[int],
    plot: Optional[str],
    poster_url: Optional[str],
    fmt: str,
    watched: bool,
    location: Optional[str],
    notes: Optional[str],
    source: Optional[str] = None,
    source_id: Optional[str] = None,
) -> int:
    poster_path = download_poster(poster_url or "") if poster_url else None

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO movies (title, year, plot, poster_url, poster_path, format, watched, location, notes, source, source_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title.strip(),
                year,
                (plot or "").strip() or None,
                (poster_url or "").strip() or None,
                poster_path,
                fmt,
                1 if watched else 0,
                (location or "").strip() or None,
                (notes or "").strip() or None,
                source,
                source_id,
            ),
        )
        return int(cur.lastrowid)

def get_movies(q: str = "", sort: str = "title_asc") -> List[sqlite3.Row]:
    where = ""
    params = []
    if q.strip():
        where = "WHERE title LIKE ?"
        params.append(f"%{q.strip()}%")

    order = "ORDER BY title COLLATE NOCASE ASC"
    if sort == "year_desc":
        order = "ORDER BY COALESCE(year, 0) DESC, title COLLATE NOCASE ASC"
    elif sort == "added_desc":
        order = "ORDER BY datetime(created_at) DESC"
    elif sort == "title_desc":
        order = "ORDER BY title COLLATE NOCASE DESC"

    with db() as conn:
        rows = conn.execute(f"SELECT * FROM movies {where} {order}", params).fetchall()
        return rows

def get_movie(movie_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()

def update_movie(movie_id: int, **fields):
    if not fields:
        return
    allowed = {"title", "year", "plot", "format", "watched", "location", "notes", "poster_url", "poster_path"}
    sets = []
    params = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(movie_id)

    with db() as conn:
        conn.execute(
            f"UPDATE movies SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            params,
        )

def delete_movie(movie_id: int):
    with db() as conn:
        conn.execute("DELETE FROM movies WHERE id=?", (movie_id,))

def get_lists() -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM lists ORDER BY name COLLATE NOCASE ASC").fetchall()

def create_list(name: str):
    with db() as conn:
        conn.execute("INSERT INTO lists (name) VALUES (?)", (name.strip(),))

def add_to_list(list_id: int, movie_id: int):
    with db() as conn:
        maxpos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM list_items WHERE list_id=?",
            (list_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO list_items (list_id, movie_id, position) VALUES (?, ?, ?)",
            (list_id, movie_id, int(maxpos) + 1),
        )

def remove_from_list(list_id: int, movie_id: int):
    with db() as conn:
        conn.execute("DELETE FROM list_items WHERE list_id=? AND movie_id=?", (list_id, movie_id))

def get_list_items(list_id: int) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT li.position, m.*
            FROM list_items li
            JOIN movies m ON m.id = li.movie_id
            WHERE li.list_id=?
            ORDER BY li.position ASC
            """,
            (list_id,),
        ).fetchall()

def move_item(list_id: int, movie_id: int, direction: str):
    items = get_list_items(list_id)
    idx = next((i for i, r in enumerate(items) if r["id"] == movie_id), None)
    if idx is None:
        return
    if direction == "up" and idx > 0:
        a = items[idx]
        b = items[idx - 1]
    elif direction == "down" and idx < len(items) - 1:
        a = items[idx]
        b = items[idx + 1]
    else:
        return

    with db() as conn:
        conn.execute(
            "UPDATE list_items SET position=? WHERE list_id=? AND movie_id=?",
            (b["position"], list_id, a["id"]),
        )
        conn.execute(
            "UPDATE list_items SET position=? WHERE list_id=? AND movie_id=?",
            (a["position"], list_id, b["id"]),
        )

# ---------------------------
# Backup (Export/Import zip)
# ---------------------------
def make_backup_zip_bytes() -> bytes:
    """Zip up the current user's movies.db + posters/ folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        db_file = Path(DB_PATH)
        posters_dir = Path(POSTERS_DIR)

        if db_file.exists():
            z.write(db_file, arcname="movies.db")

        if posters_dir.exists():
            for p in posters_dir.rglob("*"):
                if p.is_file():
                    z.write(p, arcname=str(Path("posters") / p.relative_to(posters_dir)))
    return buf.getvalue()

def restore_from_backup_zip(uploaded_bytes: bytes):
    """Restore movies.db + posters/ from a zip uploaded by the user."""
    tmp_dir = Path(USER_DIR) / "_restore_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(uploaded_bytes), "r") as z:
        z.extractall(tmp_dir)

    extracted_db = tmp_dir / "movies.db"
    extracted_posters = tmp_dir / "posters"

    if not extracted_db.exists():
        shutil.rmtree(tmp_dir)
        raise ValueError("Backup zip is missing movies.db")

    # Replace DB
    os.makedirs(Path(DB_PATH).parent, exist_ok=True)
    shutil.copy2(extracted_db, DB_PATH)

    # Replace posters folder
    if os.path.exists(POSTERS_DIR):
        shutil.rmtree(POSTERS_DIR)
    os.makedirs(POSTERS_DIR, exist_ok=True)

    if extracted_posters.exists():
        shutil.copytree(extracted_posters, POSTERS_DIR, dirs_exist_ok=True)

    shutil.rmtree(tmp_dir)

# ---------------------------
# UI setup
# ---------------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")

# Pure black theme via CSS
st.markdown(
    """
    <style>
      html, body, [class*="css"]  {
        background-color: #000000 !important;
        color: #FFFFFF !important;
      }
      .stApp { background-color: #000000; }
      header { background: rgba(0,0,0,0) !important; }
      div[data-testid="stSidebar"] { background-color: #000000; }
      .stTextInput input, .stTextArea textarea, .stSelectbox div, .stButton button, .stRadio div {
        background-color: #000000 !important;
        color: #FFFFFF !important;
        border: 1px solid #333333 !important;
      }
      .stButton button:hover { border: 1px solid #666666 !important; }
      .muted { color: #BDBDBD; }
      .card {
        border: 1px solid #222;
        border-radius: 12px;
        padding: 10px;
        background: #000;
      }
      /* Make file uploader blend in */
      section[data-testid="stFileUploader"] > div {
        background-color: #000000 !important;
        border: 1px solid #333333 !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

init_db()

st.title("🎬 Movie Shelf")
st.caption("Local. Private. Simple. (No account. Per-browser private library.)")

tabs = st.tabs(["Library", "Lists", "Add", "Settings"])

# ---------------- Library ----------------
with tabs[0]:
    colA, colB = st.columns([4, 1])
    with colA:
        q = st.text_input("Search", placeholder="Search titles…", label_visibility="collapsed", key="search_library")
    with colB:
        sort = st.selectbox(
            "Sort",
            options=[
                ("Title A→Z", "title_asc"),
                ("Title Z→A", "title_desc"),
                ("Year (new→old)", "year_desc"),
                ("Recently added", "added_desc"),
            ],
            format_func=lambda x: x[0],
            index=0,
            label_visibility="collapsed",
            key="sort_library",
        )[1]

    movies = get_movies(q=q, sort=sort)

    if not movies:
        st.markdown('<p class="muted">No movies yet. Use the Add tab.</p>', unsafe_allow_html=True)
    else:
        cols = st.columns(6)
        for i, m in enumerate(movies):
            with cols[i % 6]:
                st.markdown('<div class="card">', unsafe_allow_html=True)
                if m["poster_path"] and os.path.exists(m["poster_path"]):
                    st.image(m["poster_path"], use_container_width=True)
                else:
                    st.markdown('<p class="muted">No poster</p>', unsafe_allow_html=True)

                title_line = m["title"]
                if m["year"]:
                    title_line += f" ({m['year']})"
                st.markdown(f"**{title_line}**")
                st.markdown(
                    f"<span class='muted'>{m['format']} · {'Watched' if m['watched'] else 'Unwatched'}</span>",
                    unsafe_allow_html=True,
                )

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Open", key=f"open_{m['id']}"):
                        st.session_state["open_movie_id"] = m["id"]
                with c2:
                    if st.button("Delete", key=f"del_{m['id']}"):
                        delete_movie(m["id"])
                        st.rerun()

                st.markdown("</div>", unsafe_allow_html=True)

    # Movie detail section (one at a time)
    movie_id = st.session_state.get("open_movie_id")
    if movie_id:
        m = get_movie(int(movie_id))
        if m:
            st.divider()
            st.subheader("Movie")

            left, right = st.columns([1, 2])
            with left:
                if m["poster_path"] and os.path.exists(m["poster_path"]):
                    st.image(m["poster_path"], use_container_width=True)
                else:
                    st.markdown('<p class="muted">No poster</p>', unsafe_allow_html=True)

            with right:
                mid = int(m["id"])
                new_title = st.text_input("Title", value=m["title"], key=f"detail_title_{mid}")
                new_year = st.number_input(
                    "Year",
                    value=int(m["year"] or 0),
                    min_value=0,
                    max_value=3000,
                    key=f"detail_year_{mid}",
                )
                new_plot = st.text_area("Description", value=m["plot"] or "", height=120, key=f"detail_plot_{mid}")

                new_fmt = st.selectbox(
                    "Format",
                    ["DVD", "Blu-ray", "4K"],
                    index=["DVD", "Blu-ray", "4K"].index(
                        m["format"] if m["format"] in ["DVD", "Blu-ray", "4K"] else "Blu-ray"
                    ),
                    key=f"fmt_detail_{mid}",
                )

                new_watched = st.checkbox("Watched", value=bool(m["watched"]), key=f"detail_watched_{mid}")
                new_location = st.text_input(
                    "Location (optional)",
                    value=m["location"] or "",
                    key=f"detail_location_{mid}",
                )
                new_notes = st.text_area(
                    "Notes (optional)",
                    value=m["notes"] or "",
                    height=100,
                    key=f"detail_notes_{mid}",
                )

                c1, c2, c3 = st.columns([1, 1, 2])
                with c1:
                    if st.button("Save changes", key=f"save_{mid}"):
                        update_movie(
                            mid,
                            title=new_title.strip(),
                            year=int(new_year) if new_year else None,
                            plot=new_plot.strip() or None,
                            format=new_fmt,
                            watched=1 if new_watched else 0,
                            location=new_location.strip() or None,
                            notes=new_notes.strip() or None,
                        )
                        st.success("Saved.")
                with c2:
                    if st.button("Close", key=f"close_{mid}"):
                        st.session_state["open_movie_id"] = None
                        st.rerun()
                with c3:
                    lists = get_lists()
                    if lists:
                        list_choice = st.selectbox(
                            "Add to list",
                            ["—"] + [l["name"] for l in lists],
                            key=f"detail_addtolist_pick_{mid}",
                        )
                        if list_choice != "—" and st.button("Add", key=f"detail_addtolist_btn_{mid}"):
                            chosen = next(l for l in lists if l["name"] == list_choice)
                            add_to_list(chosen["id"], mid)
                            st.success(f"Added to {list_choice}.")
        else:
            st.session_state["open_movie_id"] = None

# ---------------- Lists ----------------
with tabs[1]:
    st.subheader("Lists")
    col1, col2 = st.columns([2, 3])

    with col1:
        new_list_name = st.text_input(
            "Create a new list",
            placeholder="e.g., Halloween",
            key="create_list_name",
        )
        if st.button("Create list", key="create_list_btn"):
            if new_list_name.strip():
                try:
                    create_list(new_list_name.strip())
                    st.success("List created.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("That list already exists.")

        lists = get_lists()
        if not lists:
            st.markdown('<p class="muted">No lists yet.</p>', unsafe_allow_html=True)
            selected = None
        else:
            list_names = [l["name"] for l in lists]
            selected_name = st.radio("Your lists", list_names, label_visibility="collapsed", key="lists_radio")
            selected = next(l for l in lists if l["name"] == selected_name)

    with col2:
        if selected:
            st.write(f"### {selected['name']}")
            items = get_list_items(selected["id"])
            if not items:
                st.markdown('<p class="muted">This list is empty.</p>', unsafe_allow_html=True)
            else:
                for r in items:
                    row = st.columns([1, 3, 1, 1, 1])
                    with row[0]:
                        if r["poster_path"] and os.path.exists(r["poster_path"]):
                            st.image(r["poster_path"], width=70)
                        else:
                            st.markdown('<span class="muted">—</span>', unsafe_allow_html=True)
                    with row[1]:
                        t = r["title"]
                        if r["year"]:
                            t += f" ({r['year']})"
                        st.markdown(f"**{t}**  \n<span class='muted'>{r['format']}</span>", unsafe_allow_html=True)

                    with row[2]:
                        if st.button("↑", key=f"up_{selected['id']}_{r['id']}"):
                            move_item(selected["id"], r["id"], "up")
                            st.rerun()
                    with row[3]:
                        if st.button("↓", key=f"down_{selected['id']}_{r['id']}"):
                            move_item(selected["id"], r["id"], "down")
                            st.rerun()
                    with row[4]:
                        if st.button("Remove", key=f"rm_{selected['id']}_{r['id']}"):
                            remove_from_list(selected["id"], r["id"])
                            st.rerun()

# ---------------- Add ----------------
with tabs[2]:
    st.subheader("Add Movie")

    mode = st.radio("Add mode", ["Quick (manual)", "Search (OMDb)"], horizontal=True, key="add_mode")

    if mode == "Quick (manual)":
        title = st.text_input("Title", key="add_title_manual")
        year = st.number_input("Year (optional)", min_value=0, max_value=3000, value=0, key="add_year_manual")
        plot = st.text_area("Description (optional)", height=120, key="add_plot_manual")
        fmt = st.selectbox("Format", ["DVD", "Blu-ray", "4K"], index=1, key="fmt_manual")
        watched = st.checkbox("Watched", value=False, key="add_watched_manual")
        location = st.text_input("Location (optional)", placeholder="e.g., Living room shelf A", key="add_loc_manual")
        notes = st.text_area("Notes (optional)", height=90, key="add_notes_manual")

        if st.button("Add to Library", key="add_btn_manual"):
            if not title.strip():
                st.warning("Please enter a title.")
            else:
                add_movie(
                    title=title,
                    year=int(year) if year else None,
                    plot=plot,
                    poster_url=None,
                    fmt=fmt,
                    watched=watched,
                    location=location,
                    notes=notes,
                )
                st.success("Added.")
                st.rerun()

    else:
        if not OMDB_API_KEY:
            st.warning("OMDb mode needs an API key. Add OMDB_API_KEY in Streamlit Secrets (or env var) and restart.")
        query = st.text_input("Search title", key="omdb_query")
        results = omdb_search(query) if query.strip() and OMDB_API_KEY else []

        if query.strip() and OMDB_API_KEY and not results:
            st.markdown('<p class="muted">No results.</p>', unsafe_allow_html=True)

        if results:
            labels = [f"{t} ({y})" for (t, y, _id) in results]
            choice = st.selectbox("Matches", labels, key="omdb_choice")
            idx = labels.index(choice)
            imdb_id = results[idx][2]

            fmt = st.selectbox("Format", ["DVD", "Blu-ray", "4K"], index=1, key="fmt_omdb")
            watched = st.checkbox("Watched", value=False, key="watched_omdb")
            location = st.text_input("Location (optional)", key="loc_omdb")
            notes = st.text_area("Notes (optional)", height=90, key="notes_omdb")

            if st.button("Add to Library (with poster)", key="add_btn_omdb"):
                meta = omdb_get(imdb_id)
                if not meta or not meta.title:
                    st.error("Could not fetch details.")
                else:
                    add_movie(
                        title=meta.title,
                        year=meta.year,
                        plot=meta.plot,
                        poster_url=meta.poster_url,
                        fmt=fmt,
                        watched=watched,
                        location=location,
                        notes=notes,
                        source=meta.source,
                        source_id=meta.source_id,
                    )
                    st.success("Added.")
                    st.rerun()

# ---------------- Settings ----------------
with tabs[3]:
    st.subheader("Settings")
    st.markdown("<p class='muted'>Per-browser private library. No account required.</p>", unsafe_allow_html=True)

    st.write("### Backup")
    backup_bytes = make_backup_zip_bytes()
    st.download_button(
        "Export Backup (.zip)",
        data=backup_bytes,
        file_name="movie_shelf_backup.zip",
        mime="application/zip",
        key="export_backup",
    )

    st.write("### Restore")
    uploaded = st.file_uploader("Import Backup (.zip)", type=["zip"], key="import_uploader")
    if uploaded is not None:
        try:
            restore_from_backup_zip(uploaded.read())
            st.success("Restored! Reloading…")
            st.rerun()
        except Exception as e:
            st.error(f"Could not restore backup: {e}")

    st.divider()

    st.write("### OMDb (optional metadata)")
    if OMDB_API_KEY:
        st.success("OMDb API key detected.")
    else:
        st.info("No OMDb key detected. Manual add still works.")
        st.markdown(
            "<p class='muted'>Tip: On Streamlit Cloud, add <b>OMDB_API_KEY</b> in app Secrets.</p>",
            unsafe_allow_html=True,
        )

    st.write("### Privacy note")
    st.markdown(
        "<p class='muted'>On Streamlit Cloud, the server may reset. Use Export Backup to keep your library safe.</p>",
        unsafe_allow_html=True,
    )


