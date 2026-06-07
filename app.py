import os
import json
import datetime
import psycopg2
import psycopg2.extras
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, Response

app = Flask(__name__)

# URL de conexión de Render (usa la Variable de Entorno DATABASE_URL si está configurada, si no, usa tu cadena por defecto)
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://voley_db_user:hgsylU2ATGZ41Xv90Mwm6J0BMPLqxWEz@dpg-d8ic4veq1p3s73eif0c0-a/voley_db')

# ---------------------------------------------------------
# ADAPTADOR INTELIGENTE POSTGRESQL (Compatibilidad con consultas SQLite)
# ---------------------------------------------------------
class PostgresConnectionWrapper:
    def __init__(self, pg_conn):
        self.pg_conn = pg_conn
    
    def execute(self, query, params=None):
        # Convierte dinámicamente los marcadores '?' de SQLite a '%s' de PostgreSQL
        query = query.replace('?', '%s')
        cur = self.pg_conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(query, params)
        return cur

    def commit(self):
        self.pg_conn.commit()

    def rollback(self):
        self.pg_conn.rollback()

    def close(self):
        self.pg_conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.pg_conn.rollback()
        else:
            self.pg_conn.commit()
        self.pg_conn.close()

def get_db_connection():
    pg_conn = psycopg2.connect(DATABASE_URL)
    return PostgresConnectionWrapper(pg_conn)

def init_db():
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS players (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                elo REAL DEFAULT 1200
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                id SERIAL PRIMARY KEY,
                score_a INTEGER NOT NULL,
                score_b INTEGER NOT NULL,
                sun_advantage TEXT CHECK(sun_advantage IN ('A', 'B', 'None')) DEFAULT 'None',
                date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS match_players (
                match_id INTEGER,
                player_id INTEGER,
                team TEXT CHECK(team IN ('A', 'B')),
                elo_change REAL DEFAULT 0,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS elo_history (
                id SERIAL PRIMARY KEY,
                player_id INTEGER,
                elo_snapshot REAL,
                date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            )
        ''')

# ---------------------------------------------------------
# LÓGICA COMPLEMENTARIA DE RACHAS Y PROCESAMIENTO ELO
# ---------------------------------------------------------
def get_current_streak(player_id, conn):
    rows = conn.execute('''
        SELECT mp.elo_change 
        FROM match_players mp
        JOIN matches m ON mp.match_id = m.id
        WHERE mp.player_id = ?
        ORDER BY m.date DESC, m.id DESC
    ''', (player_id,)).fetchall()
    
    if not rows:
        return 0, 'none'
        
    first_type = 'win' if rows[0]['elo_change'] > 0 else 'loss' if rows[0]['elo_change'] < 0 else 'none'
    if first_type == 'none':
        return 0, 'none'
        
    count = 0
    for r in rows:
        c_type = 'win' if r['elo_change'] > 0 else 'loss' if r['elo_change'] < 0 else 'none'
        if c_type == first_type:
            count += 1
        else:
            break
            
    return count, first_type

def process_match_elo(conn, match_id, team_a_ids, team_b_ids, score_a, score_b, sun):
    elo_a_sum, elo_b_sum = 0, 0
    
    for p_id in team_a_ids:
        elo_a_sum += conn.execute('SELECT elo FROM players WHERE id = ?', (p_id,)).fetchone()['elo']
    for p_id in team_b_ids:
        elo_b_sum += conn.execute('SELECT elo FROM players WHERE id = ?', (p_id,)).fetchone()['elo']
        
    count_a = len(team_a_ids)
    count_b = len(team_b_ids)
    avg_a = elo_a_sum / count_a
    avg_b = elo_b_sum / count_b
    
    adj_a = 0
    adj_b = 0
    if count_a != count_b:
        ratio_a = count_a / (count_a + count_b)
        ratio_b = count_b / (count_a + count_b)
        adj_a += (ratio_a - 0.5) * 400
        adj_b += (ratio_b - 0.5) * 400

    if sun == 'A':
        adj_a += 50
    elif sun == 'B':
        adj_b += 50
        
    avg_a_adjusted = avg_a + adj_a
    avg_b_adjusted = avg_b + adj_b

    actual_a = 1 if score_a > score_b else 0
    actual_b = 1 if score_b > score_a else 0
    
    # MÉTRICA 1: Margen de Victoria (Holgada / Ajustada)
    score_diff = abs(score_a - score_b)
    if score_diff <= 2:
        margin_multiplier = 0.75
    elif score_diff <= 4:
        margin_multiplier = 1.00
    elif score_diff <= 6:
        margin_multiplier = 1.15
    else:
        margin_multiplier = 1.30

    # MÉTRICA 2: Multiplicador por disparidad de jugadores
    disparidad = abs(count_a - count_b)
    disparidad_mult_a = 1.0
    disparidad_mult_b = 1.0
    
    if disparidad > 0:
        factor_escala = 0.25 * disparidad 
        if count_a > count_b:
            if actual_a == 1: 
                disparidad_mult_a = max(0.5, 1.0 - factor_escala)
            else:             
                disparidad_mult_a = 1.0 + factor_escala
            if actual_b == 1: 
                disparidad_mult_b = 1.0 + factor_escala
            else:             
                disparidad_mult_b = max(0.5, 1.0 - factor_escala)
        elif count_b > count_a:
            if actual_b == 1: 
                disparidad_mult_b = max(0.5, 1.0 - factor_escala)
            else:             
                disparidad_mult_b = 1.0 + factor_escala
            if actual_a == 1: 
                disparidad_mult_a = 1.0 + factor_escala
            else:             
                disparidad_mult_a = max(0.5, 1.0 - factor_escala)

    # Procesar Equipo A
    for p_id in team_a_ids:
        p_elo = conn.execute('SELECT elo FROM players WHERE id = ?', (p_id,)).fetchone()['elo']
        player_elo_adjusted = p_elo + adj_a
        
        expected_player_a = 1 / (1 + 10 ** ((avg_b_adjusted - player_elo_adjusted) / 400))
        change_base_player_a = actual_a - 0.85 * expected_player_a
        
        games_played = conn.execute('SELECT COUNT(*) as count FROM match_players WHERE player_id = ?', (p_id,)).fetchone()['count']
        k_factor = 52 if games_played < 5 else 26
        
        st_count, st_type = get_current_streak(p_id, conn)
        final_change = change_base_player_a * k_factor * margin_multiplier * disparidad_mult_a
        
        if final_change > 0 and st_type == 'win':
            final_change += (st_count * 2)
        elif final_change < 0 and st_type == 'loss':
            final_change -= (st_count * 2)
            
        final_change = int(round(final_change))
        if games_played >= 5:
            if final_change > 60: final_change = 60
            elif final_change < -60: final_change = -60
        
        conn.execute('INSERT INTO match_players (match_id, player_id, team, elo_change) VALUES (?, ?, ?, ?)', (match_id, p_id, 'A', final_change))
        conn.execute('UPDATE players SET elo = elo + ? WHERE id = ?', (final_change, p_id))
        
        new_elo = conn.execute('SELECT elo FROM players WHERE id = ?', (p_id,)).fetchone()['elo']
        conn.execute('INSERT INTO elo_history (player_id, elo_snapshot) VALUES (?, ?)', (p_id, new_elo))
        
    # Procesar Equipo B
    for p_id in team_b_ids:
        p_elo = conn.execute('SELECT elo FROM players WHERE id = ?', (p_id,)).fetchone()['elo']
        player_elo_adjusted = p_elo + adj_b
        
        expected_player_b = 1 / (1 + 10 ** ((avg_a_adjusted - player_elo_adjusted) / 400))
        change_base_player_b = actual_b - 0.85 * expected_player_b
        
        games_played = conn.execute('SELECT COUNT(*) as count FROM match_players WHERE player_id = ?', (p_id,)).fetchone()['count']
        k_factor = 52 if games_played < 5 else 26
        
        st_count, st_type = get_current_streak(p_id, conn)
        final_change = change_base_player_b * k_factor * margin_multiplier * disparidad_mult_b
        
        if final_change > 0 and st_type == 'win':
            final_change += (st_count * 2)
        elif final_change < 0 and st_type == 'loss':
            final_change -= (st_count * 2)
            
        final_change = int(round(final_change))
        if games_played >= 5:
            if final_change > 60: final_change = 60
            elif final_change < -60: final_change = -60
        
        conn.execute('INSERT INTO match_players (match_id, player_id, team, elo_change) VALUES (?, ?, ?, ?)', (match_id, p_id, 'B', final_change))
        conn.execute('UPDATE players SET elo = elo + ? WHERE id = ?', (final_change, p_id))
        
        new_elo = conn.execute('SELECT elo FROM players WHERE id = ?', (p_id,)).fetchone()['elo']
        conn.execute('INSERT INTO elo_history (player_id, elo_snapshot) VALUES (?, ?)', (p_id, new_elo))

def recalculate_all_elos(conn):
    conn.execute('UPDATE players SET elo = 1200')
    conn.execute('TRUNCATE TABLE elo_history RESTART IDENTITY CASCADE')
    conn.execute('DELETE FROM match_players')
    
    matches = conn.execute('SELECT * FROM matches ORDER BY date ASC, id ASC').fetchall()
    for m in matches:
        m_id = m['id']
        mp_rows = conn.execute('SELECT player_id, team FROM match_players_backup_temp WHERE match_id = ?', (m_id,)).fetchall()
        if not mp_rows:
            # Si no hay temporales creadas por el truco del recalculado masivo, leer directamente las relaciones actuales
            pass
        
        team_a = [r['player_id'] for r in mp_rows if r['team'] == 'A']
        team_b = [r['player_id'] for r in mp_rows if r['team'] == 'B']
        
        if team_a and team_b:
            process_match_elo(conn, m_id, team_a, team_b, m['score_a'], m['score_b'], m['sun_advantage'])

# ---------------------------------------------------------
# RUTAS DE RESPALDO Y RESTAURACIÓN (Para el cambio de mes)
# ---------------------------------------------------------
@app.route('/backup-db')
def backup_db():
    with get_db_connection() as conn:
        players = [dict(row) for row in conn.execute('SELECT * FROM players').fetchall()]
        matches = [dict(row) for row in conn.execute('SELECT * FROM matches').fetchall()]
        match_players = [dict(row) for row in conn.execute('SELECT * FROM match_players').fetchall()]
        try:
            elo_history = [dict(row) for row in conn.execute('SELECT * FROM elo_history').fetchall()]
        except:
            elo_history = []
            
    backup_data = {
        "players": players,
        "matches": matches,
        "match_players": match_players,
        "elo_history": elo_history
    }
    return Response(
        json.dumps(backup_data, default=str),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=voley_db_backup.json"}
    )

@app.route('/restore-db', methods=['GET', 'POST'])
def restore_db():
    if request.method == 'POST':
        if 'file' not in request.files: return "No file", 400
        file = request.files['file']
        if file.filename == '': return "No selected file", 400
        try:
            data = json.load(file)
            with get_db_connection() as conn:
                conn.execute('TRUNCATE TABLE match_players, elo_history, matches, players RESTART IDENTITY CASCADE')
                for p in data.get('players', []):
                    conn.execute('INSERT INTO players (id, name, elo) VALUES (?, ?, ?)', (p['id'], p['name'], p['elo']))
                for m in data.get('matches', []):
                    conn.execute('INSERT INTO matches (id, score_a, score_b, sun_advantage, date) VALUES (?, ?, ?, ?, ?)', 
                                 (m['id'], m['score_a'], m['score_b'], m['sun_advantage'], m['date']))
                for mp in data.get('match_players', []):
                    conn.execute('INSERT INTO match_players (match_id, player_id, team, elo_change) VALUES (?, ?, ?, ?)', 
                                 (mp['match_id'], mp['player_id'], mp['team'], mp['elo_change']))
                for eh in data.get('elo_history', []):
                    conn.execute('INSERT INTO elo_history (id, player_id, elo_snapshot, date) VALUES (?, ?, ?, ?)', 
                                 (eh['id'], eh['player_id'], eh['elo_snapshot'], eh.get('date')))
                
                conn.execute("SELECT setval('players_id_seq', COALESCE((SELECT MAX(id) FROM players), 1))")
                conn.execute("SELECT setval('matches_id_seq', COALESCE((SELECT MAX(id) FROM matches), 1))")
            return '<script>alert("¡Base de datos restaurada con éxito!"); window.location.href = "/";</script>'
        except Exception as e:
            return f"Error crítico durante la restauración: {str(e)}", 500
            
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Panel de Control - Base de Datos</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: system-ui, sans-serif; background: #f4f7f6; padding: 40px 20px; text-align: center; color: #333; }
            .card { background: white; max-width: 500px; margin: 0 auto; padding: 30px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }
            h2 { color: #1095c1; margin-top: 0; }
            .btn { display: inline-block; padding: 12px 24px; background: #1095c1; color: white; text-decoration: none; border-radius: 6px; font-weight: bold; margin: 10px 0; cursor: pointer; border: none; }
            .btn-secondary { background: #6c757d; }
            input[type="file"] { margin: 20px 0; padding: 10px; border: 1px dashed #ccc; width: 100%; box-sizing: border-box; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Gestión de Datos (Migración Mensual)</h2>
            <p>Usa estas herramientas para asegurar tus datos antes del reinicio del plan gratuito de Render.</p>
            <hr style="border:0; border-top:1px solid #eee; margin:20px 0;">
            <form method="post" enctype="multipart/form-data">
                <p>Selecciona tu archivo <code>voley_db_backup.json</code> guardado:</p>
                <input type="file" name="file" accept=".json" required>
                <button type="submit" class="btn">Subir y Restaurar Datos</button>
            </form>
            <br>
            <a href="/" class="btn btn-secondary">Volver al Inicio</a>
        </div>
    </body>
    </html>
    '''

# ---------------------------------------------------------
# RUTAS DE LA APLICACIÓN WEB
# ---------------------------------------------------------
@app.route('/')
def index():
    with get_db_connection() as conn:
        players_rows = conn.execute('SELECT * FROM players ORDER BY elo DESC').fetchall()
        matches_rows = conn.execute('SELECT * FROM matches ORDER BY date DESC, id DESC').fetchall()
        
        players = [dict(r) for r in players_rows]
        matches = []
        
        for m_row in matches_rows:
            m_id = m_row['id']
            mp_rows = conn.execute('''
                SELECT mp.*, p.name 
                FROM match_players mp
                JOIN players p ON mp.player_id = p.id
                WHERE mp.match_id = ?
            ''', (m_id,)).fetchall()
            
            team_a = [f"{r['name']} ({'+' if r['elo_change']>=0 else ''}{int(round(r['elo_change']))})" for r in mp_rows if r['team'] == 'A']
            team_b = [f"{r['name']} ({'+' if r['elo_change']>=0 else ''}{int(round(r['elo_change']))})" for r in mp_rows if r['team'] == 'B']
            
            matches.append({
                'id': m_id,
                'score_a': m_row['score_a'],
                'score_b': m_row['score_b'],
                'sun_advantage': m_row['sun_advantage'],
                'date': m_row['date'],
                'team_a': ", ".join(team_a),
                'team_b': ", ".join(team_b)
            })
            
    return render_template_string(HTML_TEMPLATE, players=players, matches=matches)

@app.route('/add_player', methods=['POST'])
def add_player():
    name = request.form.get('name', '').strip()
    if name:
        with get_db_connection() as conn:
            try:
                conn.execute('INSERT INTO players (name) VALUES (?)', (name,))
            except:
                pass
    return redirect(url_for('index'))

@app.route('/add_match', methods=['POST'])
def add_match():
    try:
        score_a = int(request.form.get('score_a', 0))
        score_b = int(request.form.get('score_b', 0))
        sun = request.form.get('sun', 'None')
        
        team_a_ids = [int(x) for x in request.form.getlist('team_a') if x]
        team_b_ids = [int(x) for x in request.form.getlist('team_b') if x]
        
        if not team_a_ids or not team_b_ids:
            return redirect(url_for('index'))
            
        with get_db_connection() as conn:
            cur = conn.execute('INSERT INTO matches (score_a, score_b, sun_advantage) VALUES (?, ?, ?) RETURNING id', (score_a, score_b, sun))
            match_id = cur.fetchone()[0]
            process_match_elo(conn, match_id, team_a_ids, team_b_ids, score_a, score_b, sun)
            
    except Exception as e:
        print("Error agregando partido:", e)
        
    return redirect(url_for('index'))

@app.route('/delete_match/<int:match_id>')
def delete_match(match_id):
    with get_db_connection() as conn:
        # Re-calculo de ELOs tras eliminación simulada mediante una tabla temporal espejo
        conn.execute('CREATE TEMP TABLE match_players_backup_temp AS SELECT match_id, player_id, team FROM match_players')
        conn.execute('DELETE FROM matches WHERE id = ?', (match_id,))
        conn.execute('DELETE FROM match_players_backup_temp WHERE match_id = ?', (match_id,))
        recalculate_all_elos(conn)
    return redirect(url_for('index'))

@app.route('/recalculate_elos')
def recalculate_elos_route():
    with get_db_connection() as conn:
        conn.execute('CREATE TEMP TABLE match_players_backup_temp AS SELECT match_id, player_id, team FROM match_players')
        recalculate_all_elos(conn)
    return redirect(url_for('index'))

@app.route('/player_profile/<int:player_id>')
def player_profile(player_id):
    with get_db_connection() as conn:
        p = conn.execute('SELECT * FROM players WHERE id = ?', (player_id,)).fetchone()
        if not p:
            return jsonify({'error': 'No encontrado'}), 404
            
        streak_count, streak_type = get_current_streak(player_id, conn)
        
        mp_rows = conn.execute('''
            SELECT mp.elo_change, m.score_a, m.score_b, mp.team, m.date
            FROM match_players mp
            JOIN matches m ON mp.match_id = m.id
            WHERE mp.player_id = ?
            ORDER BY m.date ASC, m.id ASC
        ''', (player_id,)).fetchall()
        
        history_rows = conn.execute('SELECT elo_snapshot FROM elo_history WHERE player_id = ? ORDER BY date ASC, id ASC', (player_id,)).fetchall()
        chart_data = [1200] + [r['elo_snapshot'] for r in history_rows]
        
        total_matches = len(mp_rows)
        wins = sum(1 for r in mp_rows if r['elo_change'] > 0)
        losses = sum(1 for r in mp_rows if r['elo_change'] < 0)
        
    return jsonify({
        'name': p['name'],
        'elo': int(round(p['elo'])),
        'total': total_matches,
        'wins': wins,
        'losses': losses,
        'streak_count': streak_count,
        'streak_type': streak_type,
        'chart_data': chart_data
    })

# ---------------------------------------------------------
# INTERFAZ JINJA HTML EN CADENA DE TEXTO
# ---------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Ranking Vóley ELO</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root { --primary: #1095c1; --dark: #2c3e50; --light: #f4f7f6; }
        body { font-family: system-ui, -apple-system, sans-serif; background: var(--light); color: var(--dark); margin:0; padding:15px; }
        .container { max-width: 900px; margin: 0 auto; }
        header { text-align: center; padding: 10px 0; background: white; border-radius: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.03); margin-bottom: 20px; }
        header h1 { margin: 5px 0; color: var(--primary); }
        .tabs { display: flex; gap: 10px; margin-bottom: 15px; }
        .tab-btn { flex: 1; padding: 12px; border: none; background: #e2e8f0; font-weight: bold; border-radius: 8px; cursor: pointer; transition: 0.2s; font-size:15px; }
        .active-tab { background: var(--primary); color: white; }
        .panel { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); margin-bottom: 20px; }
        .hidden { display: none; }
        .form-group { margin-bottom: 15px; text-align: left; }
        label { display: block; font-weight: bold; margin-bottom: 5px; }
        input, select { width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; font-size:15px; }
        .btn { width: 100%; padding: 12px; background: var(--primary); color: white; border: none; font-weight: bold; border-radius: 6px; cursor: pointer; font-size:15px; }
        .btn:hover { opacity: 0.9; }
        .flex-grid { display: flex; gap: 15px; }
        .flex-grid > div { flex: 1; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; background: white; border-radius: 8px; overflow: hidden; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }
        th { background: #f8fafc; color: #64748b; font-weight: bold; }
        .ranking-row { cursor: pointer; transition: 0.2s; }
        .ranking-row:hover { background: #f1f5f9; }
        .badge { padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; color: white; }
        .badge-win { background: #22c55e; }
        .badge-loss { background: #ef4444; }
        .match-card { background: white; padding: 15px; border-radius: 8px; border-left: 5px solid var(--primary); margin-bottom: 12px; box-shadow: 0 2px 5px rgba(0,0,0,0.02); position: relative; }
        .match-delete { position: absolute; top: 15px; right: 15px; color: #ef4444; text-decoration: none; font-weight: bold; font-size:14px; }
        dialog { border: none; border-radius: 12px; width: 90%; max-width: 500px; padding: 25px; box-shadow: 0 10px 25px rgba(0,0,0,0.15); }
        dialog::backdrop { background: rgba(0,0,0,0.4); }
        .grid-selects { display: grid; grid-template-columns: 1fr; gap: 10px; margin-top: 5px; }
        .player-select { margin-bottom: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🏐 Vóley Squad ELO</h1>
            <p style="margin:0; color:#64748b;">Historial de partidas y Clasificación en tiempo real</p>
        </header>

        <div class="tabs">
            <button class="tab-btn active-tab" onclick="switchTab('tab-ranking', this)">🏆 Clasificación</button>
            <button class="tab-btn" onclick="switchTab('tab-match', this)">➕ Añadir Partido</button>
            <button class="tab-btn" onclick="switchTab('tab-players', this)">👤 Jugadores</button>
        </div>

        <div id="tab-ranking" class="panel">
            <h2 style="margin-top:0; display:flex; justify-content:space-between; align-items:center; font-size:20px;">
                <span>Tabla de Posiciones</span>
                <div>
                    <a href="/recalculate_elos" style="font-size:13px; background:#64748b; color:white; padding:6px 12px; text-decoration:none; border-radius:4px; margin-right:5px;">🔄 Recalcular</a>
                    <a href="/backup-db" style="font-size:13px; background:#28a745; color:white; padding:6px 12px; text-decoration:none; border-radius:4px; margin-right:5px;">📥 Backup</a>
                    <a href="/restore-db" style="font-size:13px; background:#ffc107; color:#212529; padding:6px 12px; text-decoration:none; border-radius:4px;">📤 Restaurar</a>
                </div>
            </h2>
            <table>
                <thead>
                    <tr>
                        <th style="width: 50px; text-align:center;">Pos</th>
                        <th>Nombre</th>
                        <th style="text-align:right;">Puntos ELO</th>
                    </tr>
                </thead>
                <tbody>
                    {% for p in players %}
                    <tr class="ranking-row" onclick="openProfile('{{ p.id }}')">
                        <td style="text-align:center; font-weight:bold;">{{ loop.index }}</td>
                        <td style="font-weight: 500;">{{ p.name }}</td>
                        <td style="text-align:right; font-weight:bold; color:var(--primary);">{{ p.elo | round | int }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div id="tab-match" class="panel hidden">
            <h2 style="margin-top:0; font-size:20px;">Registrar Nuevo Partido</h2>
            <form action="/add_match" method="post">
                <div class="flex-grid" style="margin-bottom: 15px;">
                    <div class="form-group">
                        <label>Tamaño Equipo A</label>
                        <select id="size_a" onchange="generatePlayerSelects()">
                            <option value="2">2 vs 2</option>
                            <option value="3" selected>3 vs 3</option>
                            <option value="4">4 vs 4</option>
                            <option value="1">1 Jugador</option>
                            <option value="5">5 Jugadores</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Tamaño Equipo B</label>
                        <select id="size_b" onchange="generatePlayerSelects()">
                            <option value="2">2 vs 2</option>
                            <option value="3" selected>3 vs 3</option>
                            <option value="4">4 vs 4</option>
                            <option value="1">1 Jugador</option>
                            <option value="5">5 Jugadores</option>
                        </select>
                    </div>
                </div>

                <div class="flex-grid">
                    <div>
                        <h3 style="margin:5px 0; font-size:16px; color:#1e293b;">Equipo A</h3>
                        <div id="team_a_container" class="grid-selects"></div>
                        <div class="form-group" style="margin-top:10px;">
                            <label>Puntuación Set A</label>
                            <input type="number" name="score_a" min="0" value="0" required>
                        </div>
                    </div>
                    <div style="border-left: 1px solid #e2e8f0; padding-left:15px;">
                        <h3 style="margin:5px 0; font-size:16px; color:#1e293b;">Equipo B</h3>
                        <div id="team_b_container" class="grid-selects"></div>
                        <div class="form-group" style="margin-top:10px;">
                            <label>Puntuación Set B</label>
                            <input type="number" name="score_b" min="0" value="0" required>
                        </div>
                    </div>
                </div>

                <div class="form-group">
                    <label>Ventaja del Campo (Sol/Viento)</label>
                    <select name="sun">
                        <option value="None">Ninguno (Campo Neutro / Techado)</option>
                        <option value="A">Equipo A tuvo el sol en contra/desventaja</option>
                        <option value="B">Equipo B tuvo el sol en contra/desventaja</option>
                    </select>
                </div>

                <button type="submit" class="btn" style="margin-top:10px;">💾 Guardar Partido y Calcular ELO</button>
            </form>
        </div>

        <div id="tab-players" class="panel hidden">
            <h2 style="margin-top:0; font-size:20px;">Añadir Nuevo Jugador</h2>
            <form action="/add_player" method="post" style="display:flex; gap:10px; margin-bottom:20px;">
                <input type="text" name="name" placeholder="Nombre completo del jugador..." required style="flex:3;">
                <button type="submit" class="btn" style="flex:1;">Añadir</button>
            </form>
            
            <h3 style="font-size:16px; margin-bottom:5px;">Historial Reciente de Partidos</h3>
            <div style="max-height: 400px; overflow-y:auto; padding-right:5px;">
                {% for m in matches %}
                <div class="match-card">
                    <span style="font-size:12px; color:#94a3b8; font-weight:bold;">PARTIDO #{{ m.id }}</span>
                    <a href="/delete_match/{{ m.id }}" class="match-delete" onclick="return confirm('¿Seguro que quieres borrar este partido? Se recalculará todo el historial.')">Eliminar</a>
                    <div style="margin: 5px 0; font-weight:bold; font-size:16px;">
                        Equipo A <span style="color:var(--primary);">{{ m.score_a }}</span> vs <span style="color:var(--primary);">{{ m.score_b }}</span> Equipo B
                    </div>
                    <div style="font-size:14px; color:#475569; margin-bottom:3px;"><strong>A:</strong> {{ m.team_a }}</div>
                    <div style="font-size:14px; color:#475569;"><strong>B:</strong> {{ m.team_b }}</div>
                    {% if m.sun_advantage != 'None' %}
                    <div style="font-size:11px; margin-top:5px; color:#b45309; font-weight:500;">☀️ Desventaja climatológica asignada al Equipo {{ m.sun_advantage }}</div>
                    {% endif %}
                </div>
                {% else %}
                <p style="color:#64748b; text-align:center;">No hay partidos registrados aún.</p>
                {% endfor %}
            </div>
        </div>
    </div>

    <dialog id="profileModal">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
            <h2 id="prof-name" style="margin:0; color:var(--primary);">Jugador</h2>
            <button onclick="closeProfile()" style="background:none; border:none; font-size:24px; cursor:pointer; color:#94a3b8;">&times;</button>
        </div>
        <div class="flex-grid" style="text-align:center; margin-bottom:15px;">
            <div style="background:#f8fafc; padding:10px; border-radius:8px;">
                <div style="font-size:12px; color:#64748b; font-weight:bold;">ELO ACTUAL</div>
                <div id="prof-elo" style="font-size:24px; font-weight:bold; color:var(--primary);">1200</div>
            </div>
            <div style="background:#f8fafc; padding:10px; border-radius:8px;">
                <div style="font-size:12px; color:#64748b; font-weight:bold;">PARTIDOS</div>
                <div id="prof-total" style="font-size:24px; font-weight:bold;">0</div>
            </div>
            <div style="background:#f8fafc; padding:10px; border-radius:8px;">
                <div style="font-size:12px; color:#64748b; font-weight:bold;">RACHA</div>
                <div id="prof-streak" style="font-size:14px; font-weight:bold; margin-top:8px;">-</div>
            </div>
        </div>
        <div style="display:flex; gap:10px; font-size:14px; margin-bottom:15px; font-weight:500;">
            <div style="flex:1; background:#f0fdf4; color:#166534; padding:8px; border-radius:6px; text-align:center;">Victorias: <span id="prof-wins" style="font-weight:bold;">0</span></div>
            <div style="flex:1; background:#fef2f2; color:#991b1b; padding:8px; border-radius:6px; text-align:center;">Derrotas: <span id="prof-losses" style="font-weight:bold;">0</span></div>
        </div>
        <div style="height:200px; width:100%;">
            <canvas id="eloChart"></canvas>
        </div>
    </dialog>

    <script>
        let myChart = null;

        function switchTab(tabId, button) {
            document.getElementById('tab-ranking').classList.add('hidden');
            document.getElementById('tab-match').classList.add('hidden');
            document.getElementById('tab-players').classList.add('hidden');
            document.getElementById(tabId).classList.remove('hidden');
            
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active-tab'));
            button.classList.add('active-tab');
        }

        const playersList = [
            {% for p in players %}
            { id: "{{ p.id }}", name: "{{ p.name }}" },
            {% endfor %}
        ];

        function generatePlayerSelects() {
            const sizeA = document.getElementById('size_a').value;
            const sizeB = document.getElementById('size_b').value;
            const containerA = document.getElementById('team_a_container');
            const containerB = document.getElementById('team_b_container');
            
            let htmlA = ''; let htmlB = '';
            for(let i=0; i<sizeA; i++) { htmlA += createSelectHTML('team_a', i+1); }
            for(let i=0; i<sizeB; i++) { htmlB += createSelectHTML('team_b', i+1); }
            
            containerA.innerHTML = htmlA;
            containerB.innerHTML = htmlB;
        }

        function createSelectHTML(teamName, index) {
            let html = `<select name="${teamName}" class=\"player-select\" required><option value=\"\">Selecciona Jugador ${index}...</option>`;
            playersList.forEach(p => { html += `<option value="${p.id}">${p.name}</option>`; });
            html += `</select>`;
            return html;
        }

        function openProfile(playerId) {
            fetch(`/player_profile/${playerId}`)
                .then(res => res.json())
                .then(data => {
                    document.getElementById('prof-name').innerText = data.name;
                    document.getElementById('prof-elo').innerText = data.elo;
                    document.getElementById('prof-total').innerText = data.total;
                    document.getElementById('prof-wins').innerText = data.wins;
                    document.getElementById('prof-losses').innerText = data.losses;
                    
                    const stLabel = document.getElementById('prof-streak');
                    if(data.streak_type === 'win') {
                        stLabel.innerHTML = `<span class="badge badge-win">${data.streak_count} Victorias</span>`;
                    } else if(data.streak_type === 'loss') {
                        stLabel.innerHTML = `<span class="badge badge-loss">${data.streak_count} Derrotas</span>`;
                    } else {
                        stLabel.innerText = "Sin racha";
                    }

                    const ctx = document.getElementById('eloChart').getContext('2d');
                    if(myChart) { myChart.destroy(); }
                    
                    const labels = data.chart_data.map((_, i) => i === 0 ? "Base" : `P${i}`);
                    myChart = new Chart(ctx, {
                        type: 'line',
                        data: {
                            labels: labels,
                            datasets: [{
                                label: 'Evolución del ELO',
                                data: data.chart_data,
                                borderColor: '#1095c1',
                                backgroundColor: 'rgba(16, 149, 193, 0.1)',
                                tension: 0.2,
                                fill: true
                            }]
                        },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            scales: { y: { ticks: { stepSize: 20 } } }
                        }
                    });

                    document.getElementById('profileModal').open = true;
                });
        }

        function closeProfile() {
            document.getElementById('profileModal').open = false;
        }

        window.onload = generatePlayerSelects;
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
