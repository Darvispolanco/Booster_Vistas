import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from datetime import datetime
import pytz

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "clave_secreta_123")

# ============ CONFIG ============
PLATAFORMAS = {
    'tiktok': {'dominios': ['tiktok.com'], 'regex': r'/video/(\d+)'},
    'youtube': {'dominios': ['youtube.com', 'youtu.be'], 'regex': [r'v=([A-Za-z0-9_-]{11})', r'youtu\.be/([A-Za-z0-9_-]{11})']},
    'instagram': {'dominios': ['instagram.com'], 'regex': r'/(reel|p)/([A-Za-z0-9_-]+)'},
}
PALABRAS_BLOQUEADAS = ['xxx','porn','onlyfans','casino','apuesta']

def conectar_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def crear_base_datos():
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            telefono TEXT UNIQUE NOT NULL,
            contrasena TEXT NOT NULL,
            fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            es_admin INTEGER DEFAULT 0,
            creditos INTEGER DEFAULT 5
        );
        CREATE TABLE IF NOT EXISTS videos (
            id SERIAL PRIMARY KEY,
            telefono_creador TEXT NOT NULL,
            video_id TEXT NOT NULL,
            plataforma TEXT NOT NULL,
            titulo TEXT,
            categoria TEXT DEFAULT 'general',
            vistas INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            estado TEXT DEFAULT 'activo',
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(video_id, plataforma)
        );
        CREATE TABLE IF NOT EXISTS videos_propuestos (
            id SERIAL PRIMARY KEY,
            telefono_creador TEXT NOT NULL,
            nombre TEXT,
            titulo TEXT NOT NULL,
            categoria TEXT DEFAULT 'general',
            plataforma TEXT NOT NULL,
            video_id TEXT NOT NULL,
            estado TEXT DEFAULT 'pendiente',
            fecha_propuesta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        -- Migración para cuentas viejas
        ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS creditos INTEGER DEFAULT 5;
        UPDATE usuarios SET creditos = 5 WHERE creditos IS NULL;
    """)
    conn.commit()
    cur.close()
    conn.close()

crear_base_datos()

def extraer_id_video(url):
    url = url.lower().strip()
    if any(p in url for p in PALABRAS_BLOQUEADAS):
        return None, None, "Contenido prohibido"
    for plataforma, data in PLATAFORMAS.items():
        if any(d in url for d in data['dominios']):
            regex_list = data['regex'] if isinstance(data['regex'], list) else [data['regex']]
            for regex in regex_list:
                m = re.search(regex, url)
                if m:
                    return m.group(1), plataforma, None
            return None, None, f"Link de {plataforma} inválido"
    return None, None, "Plataforma no soportada. Usa TikTok, YouTube o Instagram"

# ============ RUTAS LOGIN ============
@app.route("/")
def index():
    if "usuario" in session:
        return redirect(url_for("videos_feed"))
    return render_template("index.html")

@app.route("/registro", methods=["POST"])
def registro():
    datos = request.get_json(silent=True) or request.form
    nombre = datos.get("nombre","").strip()
    telefono = datos.get("telefono","").strip()
    contrasena = datos.get("contrasena") or datos.get("password","")
    if not all([nombre, telefono, contrasena]):
        return jsonify({"success": False, "error": "Faltan campos"}), 400
    conn = conectar_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("INSERT INTO usuarios (nombre, telefono, contrasena, creditos) VALUES (%s,%s,%s,5) RETURNING *",
                    (nombre, telefono, generate_password_hash(contrasena)))
        usuario = cur.fetchone()
        conn.commit()
        session["usuario"] = {"id": usuario["id"], "telefono": usuario["telefono"], "nombre": usuario["nombre"]}
        return jsonify({"success": True, "redirect": "/videos"})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": "Teléfono ya registrado"}), 400
    finally:
        cur.close()
        conn.close()

@app.route("/login", methods=["POST"])
def login():
    datos = request.get_json(silent=True) or request.form
    telefono = datos.get("telefono","").strip()
    contrasena = datos.get("password") or datos.get("contrasena","")
    conn = conectar_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM usuarios WHERE telefono=%s", (telefono,))
    usuario = cur.fetchone()
    cur.close()
    conn.close()
    if usuario and check_password_hash(usuario["contrasena"], contrasena):
        session["usuario"] = {"id": usuario["id"], "telefono": usuario["telefono"], "nombre": usuario["nombre"], "es_admin": usuario["es_admin"]}
        return jsonify({"success": True, "redirect": "/videos"})
    return jsonify({"success": False, "error": "Credenciales incorrectas"}), 401

# ============ NUEVA LOGICA BOOSTER COLABORATIVA ============
@app.route("/videos")
def videos_feed():
    if "usuario" not in session:
        return redirect("/")
    categoria = request.args.get("categoria","general").lower()
    conn = conectar_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # Solo muestra videos de gente con créditos > 0 y que no sean míos
    cur.execute("""
        SELECT v.*, u.nombre, u.creditos as creditos_creador FROM videos v
        LEFT JOIN usuarios u ON v.telefono_creador = u.telefono
        WHERE v.estado='activo' 
        AND LOWER(v.categoria)=%s 
        AND v.telefono_creador != %s
        AND COALESCE(u.creditos, 0) > 0
        ORDER BY v.vistas ASC, v.fecha_creacion DESC LIMIT 20
    """, (categoria, session["usuario"]["telefono"]))
    lista = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("feed_videos.html", videos=lista, cat_actual=categoria)

@app.route("/proponer_video", methods=["GET"])
def pagina_proponer():
    if "usuario" not in session:
        return redirect("/")
    return render_template("proponer_video.html")

@app.route("/proponer_video", methods=["POST"])
def proponer_video():
    if "usuario" not in session:
        return jsonify({"error": "No autorizado"}), 401
    data = request.get_json()
    url_original = data.get("url_video","").strip()
    titulo = data.get("titulo","").strip()
    categoria = data.get("categoria","general").lower()
    
    video_id, plataforma, error = extraer_id_video(url_original)
    if error:
        return jsonify({"error": error}), 400
    
    conn = conectar_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # Verificar si ya existe
    cur.execute("SELECT id FROM videos WHERE video_id=%s AND plataforma=%s", (video_id, plataforma))
    if cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": "Este video ya está en Booster"}), 400

    # Revisar créditos del usuario
    cur.execute("SELECT creditos FROM usuarios WHERE telefono=%s", (session["usuario"]["telefono"],))
    user = cur.fetchone()
    if not user or user["creditos"] <= 0:
        cur.close()
        conn.close()
        return jsonify({"error": "Sin créditos. Ve 3 videos para ganar créditos y poder publicar"}), 400

    cur.execute("INSERT INTO videos (telefono_creador, titulo, categoria, plataforma, video_id, vistas) VALUES (%s,%s,%s,%s,%s,0) RETURNING id",
                (session["usuario"]["telefono"], titulo, categoria, plataforma, video_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True, "message": "¡Video publicado! Tienes 5 créditos para empezar"})

@app.route("/api/ver_video/<int:id>", methods=["POST"])
def ver_video(id):
    if "usuario" not in session:
        return jsonify({"error": "No autorizado"}), 401
    conn = conectar_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT telefono_creador FROM videos WHERE id=%s", (id,))
    video = cur.fetchone()
    if not video:
        cur.close()
        conn.close()
        return jsonify({"error": "Video no existe"}), 404
    
    telefono_viewer = session["usuario"]["telefono"]
    telefono_creador = video["telefono_creador"]
    
    # No ganar créditos por ver tu propio video
    if telefono_viewer == telefono_creador:
        cur.close()
        conn.close()
        return jsonify({"ok": True, "creditos": 0, "msg": "No ganas por ver tu propio video"})

    # Transacción: viewer +1, creador -1, vista +1
    cur.execute("UPDATE usuarios SET creditos = creditos + 1 WHERE telefono=%s RETURNING creditos", (telefono_viewer,))
    nuevos_creditos = cur.fetchone()["creditos"]
    
    cur.execute("UPDATE usuarios SET creditos = GREATEST(creditos - 1, 0) WHERE telefono=%s", (telefono_creador,))
    cur.execute("UPDATE videos SET vistas = vistas + 1 WHERE id=%s", (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "creditos": nuevos_creditos})

@app.route("/api/mis_creditos")
def mis_creditos():
    if "usuario" not in session:
        return jsonify({"creditos": 0})
    conn = conectar_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT creditos FROM usuarios WHERE telefono=%s", (session["usuario"]["telefono"],))
    u = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify({"creditos": u["creditos"] if u else 0})

@app.route("/perfil")
def perfil():
    if "usuario" not in session:
        return redirect("/")
    conn = conectar_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM usuarios WHERE telefono=%s", (session["usuario"]["telefono"],))
    usuario = cur.fetchone()
    cur.execute("SELECT COUNT(*) as total, SUM(vistas) as vistas FROM videos WHERE telefono_creador=%s", (session["usuario"]["telefono"],))
    stats = cur.fetchone()
    cur.execute("SELECT * FROM videos WHERE telefono_creador=%s ORDER BY fecha_creacion DESC", (session["usuario"]["telefono"],))
    mis_videos = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("perfil.html", usuario=usuario, stats_videos={"total_videos": stats["total"], "total_vistas_booster": stats["vistas"] or 0, "videos_aprobados": stats["total"]}, urls_videos=mis_videos, videos_propuestos=stats["total"])

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)
