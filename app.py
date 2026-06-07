import os
import json
import psycopg2
import psycopg2.extras
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, Response

app = Flask(__name__)

# URL de la base de datos PostgreSQL en Render
DATABASE_URL = "postgresql://voley_db_user:hgsylU2ATGZ41Xv90Mwm6J0BMPLqxWEz@dpg-d8ic4veq1p3s73eif0c0-a/voley_db"

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS players (\n            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            elo REAL DEFAULT 1200
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            score_a INTEGER NOT NULL,
            score_b INTEGER NOT NULL,
            sun_advantage TEXT CHECK(sun_advantage IN ('A', 'B', 'None')) DEFAULT 'None',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS match_players (
            match_id INTEGER,
            player_id INTEGER,
            team TEXT CHECK(team IN ('A', 'B')),
            elo_change REAL DEFAULT 0,
            FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
            FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS elo_history (
            id SERIAL PRIMARY KEY,
            player_id INTEGER,
            elo_snapshot REAL,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
        )
    ''')
    
    try:
        cur.execute("SELECT elo_change FROM match_players LIMIT 1")
    except psycopg2.Error:
        conn.rollback()
        cur.execute("ALTER TABLE match_players ADD COLUMN elo_change REAL DEFAULT 0")

    cur.execute("SELECT id, elo FROM players")
    existing_players = cur.fetchall()
    for p in existing_players:
        p_id, p_elo = p[0], p[1]
        cur.execute("SELECT 1 FROM elo_history WHERE player_id = %s", (p_id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO elo_history (player_id, elo_snapshot) VALUES (%s, %s)", (p_id, p_elo))
            
    conn.commit()
    cur.close()
    conn.close()

# ---------------------------------------------------------
# 2. LÓGICA DE CÁLCULO ELO Y ESTADÍSTICAS
# ---------------------------------------------------------
def get_current_streak(player_id, conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('''
        SELECT m.score_a, m.score_b, mp.team 
        FROM match_players mp
        JOIN matches m ON mp.match_id = m.id
        WHERE mp.player_id = %s
        ORDER BY m.date DESC, m.id DESC
    ''', (player_id,))
    rows = cur.fetchall()
    cur.close()
    
    if not rows:
        return 0, 'none'
        
    first_won = (rows[0]['team'] == 'A' and rows[0]['score_a'] > rows[0]['score_b']) or \
                (rows[0]['team'] == 'B' and rows[0]['score_b'] > rows[0]['score_a'])
    streak_type = 'win' if first_won else 'loss'
    count = 0
    
    for r in rows:
        won = (r['team'] == 'A' and r['score_a'] > r['score_b']) or \
              (r['team'] == 'B' and r['score_b'] > r['score_a'])
        if streak_type == 'win' and won:
            count += 1
        elif streak_type == 'loss' and not won:
            count += 1
        else:
            break
    return count, streak_type

def process_match_elo(conn, match_id, team_a_ids, team_b_ids, score_a, score_b, sun):
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    elo_a_sum, elo_b_sum = 0, 0
    
    for p_id in team_a_ids:
        cur.execute('SELECT elo FROM players WHERE id = %s', (p_id,))
        elo_a_sum += cur.fetchone()['elo']
    for p_id in team_b_ids:
        cur.execute('SELECT elo FROM players WHERE id = %s', (p_id,))
        elo_b_sum += cur.fetchone()['elo']
        
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
    
    score_diff = abs(score_a - score_b)
    if score_diff <= 2:
        margin_multiplier = 0.75
    elif score_diff <= 4:
        margin_multiplier = 1.00
    elif score_diff <= 6:
        margin_multiplier = 1.15
    else:
        margin_multiplier = 1.30

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

    for p_id in team_a_ids:
        cur.execute('SELECT elo FROM players WHERE id = %s', (p_id,))
        p_elo = cur.fetchone()['elo']
        player_elo_adjusted = p_elo + adj_a
        
        expected_player_a = 1 / (1 + 10 ** ((avg_b_adjusted - player_elo_adjusted) / 400))
        change_base_player_a = actual_a - 0.85 * expected_player_a
        
        cur.execute('SELECT COUNT(*) as count FROM match_players WHERE player_id = %s', (p_id,))
        games_played = cur.fetchone()['count']
        k_factor = 52 if games_played < 5 else 26
        
        st_count, st_type = get_current_streak(p_id, conn)
        
        final_change = change_base_player_a * k_factor
        final_change = final_change * margin_multiplier * disparidad_mult_a
        
        if final_change > 0 and st_type == 'win':
            final_change += (st_count * 2)
        elif final_change < 0 and st_type == 'loss':
            final_change -= (st_count * 2)
            
        final_change = int(round(final_change))
        
        if games_played >= 5:
            if final_change > 60: final_change = 60
            elif final_change < -60: final_change = -60
        
        cur.execute('INSERT INTO match_players (match_id, player_id, team, elo_change) VALUES (%s, %s, %s, %s)', (match_id, p_id, 'A', final_change))
        cur.execute('UPDATE players SET elo = elo + %s WHERE id = %s', (final_change, p_id))
        
        cur.execute('SELECT elo FROM players WHERE id = %s', (p_id,))
        new_elo = cur.fetchone()['elo']
        cur.execute('INSERT INTO elo_history (player_id, elo_snapshot) VALUES (%s, %s)', (p_id, new_elo))
        
    for p_id in team_b_ids:
        cur.execute('SELECT elo FROM players WHERE id = %s', (p_id,))
        p_elo = cur.fetchone()['elo']
        player_elo_adjusted = p_elo + adj_b
        
        expected_player_b = 1 / (1 + 10 ** ((avg_a_adjusted - player_elo_adjusted) / 400))
        change_base_player_b = actual_b - 0.85 * expected_player_b
        
        cur.execute('SELECT COUNT(*) as count FROM match_players WHERE player_id = %s', (p_id,))
        games_played = cur.fetchone()['count']
        k_factor = 52 if games_played < 5 else 26
        
        st_count, st_type = get_current_streak(p_id, conn)
        
        final_change = change_base_player_b * k_factor
        final_change = final_change * margin_multiplier * disparidad_mult_b
        
        if final_change > 0 and st_type == 'win':
            final_change += (st_count * 2)
        elif final_change < 0 and st_type == 'loss':
            final_change -= (st_count * 2)
            
        final_change = int(round(final_change))
        
        if games_played >= 5:
            if final_change > 60: final_change = 60
            elif final_change < -60: final_change = -60
        
        cur.execute('INSERT INTO match_players (match_id, player_id, team, elo_change) VALUES (%s, %s, %s, %s)', (match_id, p_id, 'B', final_change))
        cur.execute('UPDATE players SET elo = elo + %s WHERE id = %s', (final_change, p_id))
        
        cur.execute('SELECT elo FROM players WHERE id = %s', (p_id,))
        new_elo = cur.fetchone()['elo']
        cur.execute('INSERT INTO elo_history (player_id, elo_snapshot) VALUES (%s, %s)', (p_id, new_elo))
        
    cur.close()

def recalculate_all_elos(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('SELECT * FROM matches ORDER BY date ASC, id ASC')
    db_matches = cur.fetchall()
    
    full_history = []
    for m in db_matches:
        cur.execute('SELECT player_id, team FROM match_players WHERE match_id = %s', (m['id'],))
        players = cur.fetchall()
        full_history.append({
            'id': m['id'],
            'score_a': m['score_a'],
            'score_b': m['score_b'],
            'sun_advantage': m['sun_advantage'],
            'date': m['date'],
            'players': [{'player_id': p['player_id'], 'team': p['team']} for p in players]
        })
    
    cur.execute('DELETE FROM elo_history')
    cur.execute('DELETE FROM match_players')
    cur.execute('DELETE FROM matches')
    cur.execute('UPDATE players SET elo = 1200')
    
    cur.execute("SELECT id FROM players")
    existing_players = cur.fetchall()
    for p in existing_players:
        cur.execute("INSERT INTO elo_history (player_id, elo_snapshot) VALUES (%s, 1200)", (p['id'],))
        
    for m in full_history:
        team_a_ids = [p['player_id'] for p in m['players'] if p['team'] == 'A']
        team_b_ids = [p['player_id'] for p in m['players'] if p['team'] == 'B']
        
        if team_a_ids and team_b_ids:
            cur.execute('INSERT INTO matches (id, score_a, score_b, sun_advantage, date) VALUES (%s, %s, %s, %s, %s)', 
                         (m['id'], m['score_a'], m['score_b'], m['sun_advantage'], m['date']))
            process_match_elo(conn, m['id'], team_a_ids, team_b_ids, m['score_a'], m['score_b'], m['sun_advantage'])
    cur.close()

def get_player_stats(player_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute('''
        SELECT m.id, m.score_a, m.score_b, mp.team 
        FROM match_players mp
        JOIN matches m ON mp.match_id = m.id
        WHERE mp.player_id = %s
        ORDER BY m.date DESC, m.id DESC
    ''', (player_id,))
    matches = cur.fetchall()
    
    total_matches = len(matches)
    wins = 0
    teammates_games = {}
    
    for m in matches:
        is_team_a = m['team'] == 'A'
        won = (is_team_a and m['score_a'] > m['score_b']) or (not is_team_a and m['score_b'] > m['score_a'])
        if won:
            wins += 1
        
        cur.execute('''
            SELECT player_id FROM match_players 
            WHERE match_id = %s AND team = %s AND player_id != %s
        ''', (m['id'], m['team'], player_id))
        peers = cur.fetchall()
        
        for p in peers:
            p_id = p['player_id']
            if p_id not in teammates_games:
                teammates_games[p_id] = [0, 0]
            teammates_games[p_id][0] += 1
            if won:
                teammates_games[p_id][1] += 1

    winrate = round((wins / total_matches) * 100) if total_matches > 0 else 0

    teammates_summary = []
    for t_id, counts in teammates_games.items():
        cur.execute('SELECT name FROM players WHERE id = %s', (t_id,))
        p_name = cur.fetchone()['name']
        rate = round((counts[1] / counts[0]) * 100)
        teammates_summary.append({"name": p_name, "played": counts[0], "winrate": rate})

    st_count, st_type = get_current_streak(player_id, conn)
    streak_emoji = ""
    if st_count >= 3:
        streak_emoji = f"🥵 {st_count}" if st_type == 'win' else f"🥶 {st_count}"

    cur.close()
    conn.close()
    
    return {
        "total": total_matches,
        "wins": wins,
        "losses": total_matches - wins,
        "winrate": winrate,
        "teammates": teammates_summary,
        "streak_emoji": streak_emoji
    }

# ---------------------------------------------------------
# 3. CONTROLADORES Y API
# ---------------------------------------------------------
@app.route('/')
def index():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute("SELECT MAX(id) FROM matches")
    last_match = cur.fetchone()
    last_match_id = last_match[0] if last_match and last_match[0] else None

    cur.execute('SELECT * FROM players ORDER BY elo DESC, name ASC')
    db_players = cur.fetchall()
    
    leaderboard = []
    players_list = []
    
    for p in db_players:
        players_list.append({"id": p['id'], "name": p['name']})
        stats = get_player_stats(p['id'])
        leaderboard.append({
            "id": p['id'], "name": p['name'], "elo": round(p['elo']), "prev_elo": p['elo'], "trend_emoji": "➖", **stats
        })
        
    if last_match_id and len(leaderboard) > 0:
        cur.execute("SELECT player_id, elo_change FROM match_players WHERE match_id = %s", (last_match_id,))
        changes_rows = cur.fetchall()
        changes = {row['player_id']: row['elo_change'] for row in changes_rows}
        
        for p in leaderboard:
            p['prev_elo'] = p['elo'] - changes.get(p['id'], 0.0)
            
        sorted_by_prev = sorted(leaderboard, key=lambda x: (-x['prev_elo'], x['name']))
        prev_ranks = {p['id']: idx for idx, p in enumerate(sorted_by_prev)}
        
        for idx, p in enumerate(leaderboard):
            prev_idx = prev_ranks[p['id']]
            if idx < prev_idx:
                p['trend_emoji'] = "🔺"
            elif idx > prev_idx:
                p['trend_emoji'] = "🔻"
            else:
                p['trend_emoji'] = "➖"

    cur.execute('SELECT * FROM matches ORDER BY date DESC')
    db_matches = cur.fetchall()
    match_history = []
    for m in db_matches:
        cur.execute('SELECT p.name FROM players p JOIN match_players mp ON p.id = mp.player_id WHERE mp.match_id = %s AND mp.team = \'A\'', (m['id'],))
        team_a_players = cur.fetchall()
        cur.execute('SELECT p.name FROM players p JOIN match_players mp ON p.id = mp.player_id WHERE mp.match_id = %s AND mp.team = \'B\'', (m['id'],))
        team_b_players = cur.fetchall()
        
        sun_emoji = "-"
        if m['sun_advantage'] == 'A':
            sun_emoji = "☀️ Sol en contra: Equipo B"
        elif m['sun_advantage'] == 'B':
            sun_emoji = "☀️ Sol en contra: Equipo A"

        date_str = m['date'].strftime('%Y-%m-%d') if hasattr(m['date'], 'strftime') else str(m['date']).split()[0]

        match_history.append({
            "id": m['id'],
            "date": date_str,
            "team_a": f"({len(team_a_players)}) " + ", ".join([p['name'] for p in team_a_players]),
            "team_b": f"({len(team_b_players)}) " + ", ".join([p['name'] for p in team_b_players]),
            "score_a": m['score_a'],
            "score_b": m['score_b'],
            "sun": sun_emoji
        })
        
    cur.close()
    conn.close()
    return render_template_string(HTML_TEMPLATE, leaderboard=leaderboard, players=players_list, history=match_history)

@app.route('/match_detail/<int:match_id>')
def match_detail(match_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute('SELECT * FROM matches WHERE id = %s', (match_id,))
    match = cur.fetchone()
    if not match:
        cur.close()
        conn.close()
        return jsonify({"error": "Partido no encontrado"}), 404
        
    cur.execute('''
        SELECT mp.player_id, mp.team, p.name, mp.elo_change,
               (1200 + COALESCE((SELECT SUM(mp2.elo_change) FROM match_players mp2 WHERE mp2.player_id = mp.player_id AND mp2.match_id < mp.match_id), 0)) AS elo_at_match
        FROM match_players mp
        JOIN players p ON mp.player_id = p.id
        WHERE mp.match_id = %s
    ''', (match_id,))
    players_match = cur.fetchall()
    
    team_a = []
    team_b = []
    sum_elo_a = 0
    sum_elo_b = 0
    
    for p in players_match:
        p_info = {
            "name": p['name'], 
            "elo_change": int(p['elo_change']),
            "elo_at_match": round(p['elo_at_match'])
        }
        if p['team'] == 'A':
            team_a.append(p_info)
            sum_elo_a += p['elo_at_match']
        else:
            team_b.append(p_info)
            sum_elo_b += p['elo_at_match']
            
    avg_elo_a = round(sum_elo_a / len(team_a)) if team_a else 1200
    avg_elo_b = round(sum_elo_b / len(team_b)) if team_b else 1200
    
    date_str = match['date'].strftime('%Y-%m-%d') if hasattr(match['date'], 'strftime') else str(match['date']).split()[0]
    
    cur.close()
    conn.close()
    return jsonify({
        "id": match['id'],
        "score_a": match['score_a'],
        "score_b": match['score_b'],
        "sun_advantage": match['sun_advantage'],
        "date": date_str,
        "team_a": team_a,
        "team_b": team_b,
        "avg_elo_a": avg_elo_a,
        "avg_elo_b": avg_elo_b
    })

@app.route('/player_profile/<int:player_id>')
def player_profile(player_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute('SELECT * FROM players WHERE id = %s', (player_id,))
    player = cur.fetchone()
    if not player:
        cur.close()
        conn.close()
        return jsonify({"error": "Jugador no encontrado"}), 404
        
    stats = get_player_stats(player_id)
    
    cur.execute('''
        SELECT m.id, m.date, m.score_a, m.score_b, mp.team, mp.elo_change 
        FROM match_players mp
        JOIN matches m ON mp.match_id = m.id
        WHERE mp.player_id = %s
        ORDER BY m.date DESC
    ''', (player_id,))
    raw_history = cur.fetchall()
    
    match_list = []
    for rh in raw_history:
        is_a = rh['team'] == 'A'
        won = (is_a and rh['score_a'] > rh['score_b']) or (not is_a and rh['score_b'] > rh['score_a'])
        date_str = rh['date'].strftime('%Y-%m-%d') if hasattr(rh['date'], 'strftime') else str(rh['date']).split()[0]
        
        match_list.append({
            "id": rh['id'],
            "date": date_str,
            "score": f"{rh['score_a']} - {rh['score_b']}",
            "team": rh['team'],
            "result": "Victoria" if won else "Derrota",
            "change": int(rh['elo_change']) if rh['elo_change'] is not None else 0
        })

    cur.execute('SELECT elo_snapshot FROM elo_history WHERE player_id = %s ORDER BY date ASC', (player_id,))
    elo_snaps = cur.fetchall()
    chart_data = [1200] + [row['elo_snapshot'] for row in elo_snaps]

    cur.close()
    conn.close()
    return jsonify({
        "name": player['name'],
        "elo": round(player['elo']),
        "stats": stats,
        "matches": match_list,
        "chart_data": chart_data
    })

@app.route('/add_player', methods=['POST'])
def add_player():
    name = request.form.get('name').strip()
    if name:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO players (name) VALUES (%s) RETURNING id', (name,))
            player_id = cur.fetchone()[0]
            cur.execute('INSERT INTO elo_history (player_id, elo_snapshot) VALUES (%s, 1200)', (player_id,))
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()
        finally:
            cur.close()
            conn.close()
    return redirect(url_for('index'))

@app.route('/edit_player', methods=['POST'])
def edit_player():
    player_id = request.form.get('player_id')
    new_name = request.form.get('new_name').strip()
    if player_id and new_name:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute('UPDATE players SET name = %s WHERE id = %s', (new_name, player_id))
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()
        finally:
            cur.close()
            conn.close()
    return redirect(url_for('index'))

@app.route('/add_match', methods=['POST'])
def add_match():
    team_a_ids = list(set([int(pid) for pid in request.form.getlist('team_a') if pid]))
    team_b_ids = list(set([int(pid) for pid in request.form.getlist('team_b') if pid]))
    
    if set(team_a_ids).intersection(set(team_b_ids)):
        return "<script>alert('Error: Un jugador no puede estar en ambos equipos.'); window.history.back();</script>", 400

    score_a = int(request.form.get('score_a', 0))
    score_b = int(request.form.get('score_b', 0))
    
    sun_against_a = request.form.get('sun_against_a')
    sun_against_b = request.form.get('sun_against_b')
    if sun_against_a == 'A':
        sun = 'B'
    elif sun_against_b == 'B':
        sun = 'A'
    else:
        sun = 'None'
    
    if team_a_ids and team_b_ids:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO matches (score_a, score_b, sun_advantage) VALUES (%s, %s, %s) RETURNING id', (score_a, score_b, sun))
        match_id = cur.fetchone()[0]
        
        process_match_elo(conn, match_id, team_a_ids, team_b_ids, score_a, score_b, sun)
        
        conn.commit()
        cur.close()
        conn.close()
        
    return redirect(url_for('index'))

@app.route('/delete_match/<int:match_id>', methods=['POST'])
def delete_match(match_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM matches WHERE id = %s', (match_id,))
    cur.execute('DELETE FROM match_players WHERE match_id = %s', (match_id,))
    recalculate_all_elos(conn)
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('index'))

@app.route('/edit_match/<int:match_id>', methods=['POST'])
def edit_match(match_id):
    score_a = int(request.form.get('score_a', 0))
    score_b = int(request.form.get('score_b', 0))
    sun_against_a = request.form.get('sun_against_a')
    sun_against_b = request.form.get('sun_against_b')
    
    if sun_against_a == 'A':
        sun = 'B'
    elif sun_against_b == 'B':
        sun = 'A'
    else:
        sun = 'None'
        
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE matches SET score_a = %s, score_b = %s, sun_advantage = %s WHERE id = %s', 
                 (score_a, score_b, sun, match_id))
    recalculate_all_elos(conn)
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('index'))


# =========================================================
# RUTAS DE BACKUP (JSON MIGRATION PLAN)
# =========================================================
@app.route('/download_backup')
def download_backup():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        cur.execute("SELECT id, name, elo FROM players")
        players = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT id, score_a, score_b, sun_advantage, date FROM matches")
        matches = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get('date') and hasattr(d['date'], 'isoformat'):
                d['date'] = d['date'].isoformat()
            else:
                d['date'] = str(d['date'])
            matches.append(d)
            
        cur.execute("SELECT id, player_id, elo_snapshot, date FROM elo_history")
        elo_history = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get('date') and hasattr(d['date'], 'isoformat'):
                d['date'] = d['date'].isoformat()
            else:
                d['date'] = str(d['date'])
            elo_history.append(d)
            
        cur.execute("SELECT match_id, player_id, team, elo_change FROM match_players")
        match_players = [dict(row) for row in cur.fetchall()]
        
        backup_data = {
            "players": players,
            "matches": matches,
            "match_players": match_players,
            "elo_history": elo_history
        }
        
        cur.close()
        conn.close()
        
        json_data = json.dumps(backup_data, indent=4)
        return Response(
            json_data,
            mimetype="application/json",
            headers={"Content-disposition": "attachment; filename=voley_db_backup.json"}
        )
    except Exception as e:
        return f"Error al generar la copia de seguridad: {str(e)}", 500


@app.route('/restore_backup', methods=['POST'])
def restore_backup():
    if 'backup_file' not in request.files:
        return "No se ha subido ningún archivo.", 400
        
    file = request.files['backup_file']
    if file.filename == '':
        return "Archivo no válido.", 400
        
    try:
        backup_data = json.load(file)
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("TRUNCATE match_players, elo_history, matches, players RESTART IDENTITY CASCADE;")
        
        for p in backup_data.get('players', []):
            cur.execute("INSERT INTO players (id, name, elo) VALUES (%s, %s, %s)", (p['id'], p['name'], p['elo']))
            
        for m in backup_data.get('matches', []):
            cur.execute("INSERT INTO matches (id, score_a, score_b, sun_advantage, date) VALUES (%s, %s, %s, %s, %s)", 
                        (m['id'], m['score_a'], m['score_b'], m['sun_advantage'], m['date']))
            
        for mp in backup_data.get('match_players', []):
            cur.execute("INSERT INTO match_players (match_id, player_id, team, elo_change) VALUES (%s, %s, %s, %s)", 
                        (mp['match_id'], mp['player_id'], mp['team'], mp['elo_change']))
            
        for eh in backup_data.get('elo_history', []):
            cur.execute("INSERT INTO elo_history (id, player_id, elo_snapshot, date) VALUES (%s, %s, %s, %s)", 
                        (eh['id'], eh['player_id'], eh['elo_snapshot'], eh['date']))
            
        cur.execute("SELECT setval('players_id_seq', COALESCE((SELECT MAX(id) FROM players), 1))")
        cur.execute("SELECT setval('matches_id_seq', COALESCE((SELECT MAX(id) FROM matches), 1))")
        cur.execute("SELECT setval('elo_history_id_seq', COALESCE((SELECT MAX(id) FROM elo_history), 1))")
        
        conn.commit()
        cur.close()
        conn.close()
        
        return redirect(url_for('index'))
    except Exception as e:
        return f"Error al restaurar los datos: {str(e)}", 500


# ---------------------------------------------------------
# 4. INTERFAZ EN HTML INTEGRADA
# ---------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Beach Volley ELO Tracker</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@1/css/pico.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { padding: 20px; }
        .tab-content { display: none; }
        .tab-content.active { display: block; animation: fadeIn 0.3s; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        nav { border-bottom: 2px solid #ddd; margin-bottom: 30px; padding-bottom: 10px;}
        nav a { cursor: pointer; margin-right: 15px; font-weight: bold; color: #555; text-decoration: none;}
        nav a.active-tab { color: #1095c1; border-bottom: 2px solid #1095c1; padding-bottom: 12px;}
        .grid-forms { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .player-select { margin-bottom: 10px; }
        .placement-badge { background-color: #ff9800; color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold;}
        .streak-badge { font-weight: bold; font-size: 0.9rem; margin-right: 6px; }
        th.sortable { cursor: pointer; position: relative; }
        th.sortable:hover { background-color: #eaeaea; }
        .player-link { color: #1095c1; cursor: pointer; text-decoration: underline; }
        .win-row { background-color: rgba(76, 175, 80, 0.15) !important; }
        .loss-row { background-color: rgba(244, 67, 54, 0.15) !important; }
        dialog article { max-width: 900px; width: 90%; }
        .split-profile { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 15px; }
        
        .volley-court {
            display: flex; 
            align-items: stretch; 
            justify-content: space-between; 
            background-color: #fce4ec; 
            padding: 25px; 
            border-radius: 12px; 
            min-height: 220px; 
            position: relative; 
            border: 3px solid #c2185b;
        }
        .court-side {
            flex: 1; 
            display: flex; 
            flex-direction: column; 
            justify-content: center; 
            align-items: center;
        }
        .volley-net {
            width: 8px; 
            background: repeating-linear-gradient(0deg, #ffffff, #ffffff 8px, #333333 8px, #333333 16px); 
            border-left: 2px solid #000; 
            border-right: 2px solid #000; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            position: relative;
        }
        .net-label {
            position: absolute; 
            background: #ffeb3b; 
            color: #000; 
            font-size: 0.65rem; 
            font-weight: bold; 
            padding: 1px 6px; 
            border-radius: 4px; 
            transform: rotate(-90deg); 
            white-space: nowrap;
            border: 1px solid #000;
        }
    </style>
</head>
<body>
    <main class="container">
        <header>
            <h1 style="margin-bottom: 5px;">🏐 Voley Playa ELO</h1>
        </header>

        <nav>
            <a onclick="showTab('ranking', this)" class="active-tab">🏆 Ránking y Métricas</a>
            <a onclick="showTab('add_match', this)">📝 Añadir Resultado</a>
            <a onclick="showTab('history', this)">📖 Historial de Partidos</a>
            <a onclick="showTab('players', this)">👥 Gestión</a>
        </nav>

        <section id="ranking" class="tab-content active">
            <h2>Clasificación General</h2>
            <figure>
                <table role="grid" id="leaderboardTable">
                    <thead>
                        <tr>
                            <th class="sortable" onclick="sortTable(0, true)">Pos ⇅</th>
                            <th class="sortable" onclick="sortTable(1, false)">Jugador ⇅</th>
                            <th class="sortable" onclick="sortTable(2, true)">🔥 ELO ⇅</th>
                            <th class="sortable" onclick="sortTable(3, true)">PJ ⇅</th>
                            <th class="sortable" onclick="sortTable(4, true)">V / D ⇅</th>
                            <th class="sortable" onclick="sortTable(5, true)">📈 WinRate ⇅</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for p in leaderboard %}
                        <tr>
                            <td><strong>{{ loop.index }}</strong></td>
                            <td style="display: flex; align-items: center; gap: 8px;">
                                <span style="font-size: 0.95rem; width: 20px; text-align: center;" title="Tendencia tras último partido">{{ p.trend_emoji }}</span>
                                <span class="player-link" style="display: inline-block; min-width: 140px; font-weight: 500;" onclick="openProfile({{ p.id }})">{{ p.name }}</span>
                                {% if p.streak_emoji %}
                                <span class="streak-badge" style="margin: 0;">{{ p.streak_emoji }}</span>
                                {% endif %}
                                {% if p.total < 5 %}
                                <span class="placement-badge">PROBACIÓN {{p.total}}/5</span>
                                {% endif %}
                            </td>
                            <td><strong>{{ p.elo }}</strong></td>
                            <td>{{ p.total }}</td>
                            <td><span style="color: green;">{{ p.wins }}</span> / <span style="color: red;">{{ p.losses }}</span></td>
                            <td><strong>{{ p.winrate }}%</strong></td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </figure>
        </section>

        <section id="add_match" class="tab-content">
            <h2>Registrar Nuevo Partido</h2>
            <form action="/add_match" method="POST" onsubmit="return validateTeams()">
                <div class="grid">
                    <label>Jugadores Equipo A:
                        <input type="number" id="size_a" min="1" max="6" value="4" onchange="generatePlayerSelects()">
                    </label>
                    <label>Jugadores Equipo B:
                        <input type="number" id="size_b" min="1" max="6" value="4" onchange="generatePlayerSelects()">
                    </label>
                </div>
                
                <div class="grid">
                    <div>
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <h4 style="color: #d81b60; margin:0;">Equipo A</h4>
                            <label style="margin:0; font-size:0.9rem;"><input type="checkbox" id="sun_against_a" name="sun_against_a" value="A" onchange="toggleSun('A')"> ☀️ Sol en contra</label>
                        </div>
                        <hr>
                        <div id="team_a_container"></div>
                        <label>Puntos Equipo A
                            <input type="number" name="score_a" min="0" value="0" required>
                        </label>
                    </div>
                    <div>
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <h4 style="color: #1e88e5; margin:0;">Equipo B</h4>
                            <label style="margin:0; font-size:0.9rem;"><input type="checkbox" id="sun_against_b" name="sun_against_b" value="B" onchange="toggleSun('B')"> ☀️ Sol en contra</label>
                        </div>
                        <hr>
                        <div id="team_b_container"></div>
                        <label>Puntos Equipo B
                            <input type="number" name="score_b" min="0" value="0" required>
                        </label>
                    </div>
                </div>
                <br>
                <button type="submit" class="secondary">Guardar Resultado</button>
            </form>
        </section>

        <section id="history" class="tab-content">
            <h2>Historial de Partidos</h2>
            <figure>
                <table role="grid">
                    <thead>
                        <tr>
                            <th>Fecha</th><th>Equipo A</th><th>Puntos</th><th>Equipo B</th><th>Sol en Contra</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for h in history %}
                        <tr onclick="openMatchDetail({{ h.id }})" style="cursor: pointer;" title="Hacé click para ver detalles">
                            <td><small>{{ h.date }}</small></td>
                            <td style="color: #d81b60;">{{ h.team_a }}</td>
                            <td><strong>{{ h.score_a }} - {{ h.score_b }}</strong></td>
                            <td style="color: #1e88e5;">{{ h.team_b }}</td>
                            <td>{{ h.sun }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </figure>
        </section>

        <section id="players" class="tab-content">
            <div class="grid-forms">
                <article>
                    <h3>➕ Registrar Nuevo Jugador</h3>
                    <form action="/add_player" method="POST">
                        <input type="text" name="name" placeholder="Nombre del jugador" required>
                        <button type="submit">Registrar</button>
                    </form>
                </article>

                <article>
                    <h3>✏️ Editar Jugador Existente</h3>
                    <form action="/edit_player" method="POST">
                        <select name="player_id" required>
                            <option value="">Selecciona jugador...</option>
                            {% for p in players %}
                            <option value="{{ p.id }}">{{ p.name }}</option>
                            {% endfor %}
                        </select>
                        <input type="text" name="new_name" placeholder="Nuevo nombre" required>
                        <button type="submit" class="secondary">Actualizar Nombre</button>
                    </form>
                </article>
            </div>

            <div style="margin: 30px auto 0 auto; padding: 20px; border: 1px solid #1095c1; border-radius: 8px; background-color: #f4fafd; font-family: sans-serif;">
                <h3 style="color: #1095c1; margin-top: 0; font-size: 18px;">Gestión de Datos (Plan Gratuito Render)</h3>
                <p style="font-size: 13px; color: #555; line-height: 1.4; margin-bottom: 15px;">
                    Las bases de datos gratuitas vencen a los 30 días. Descarga tu copia de seguridad antes del <strong>7 de julio de 2026</strong>. Cuando crees una nueva base de datos el próximo mes, sube el archivo JSON aquí para restaurar todo el historial.
                </p>
                <div style="display: flex; flex-wrap: wrap; gap: 20px; align-items: center;">
                    <a href="/download_backup" style="background-color: #1095c1; color: white; padding: 10px 18px; text-decoration: none; border-radius: 4px; font-weight: bold; font-size: 14px; display: inline-block;">
                        📥 Descargar Copia Actual (JSON)
                    </a>
                    <form action="/restore_backup" method="POST" enctype="multipart/form-data" style="display: flex; align-items: center; gap: 10px; margin: 0;">
                        <input type="file" id="backup_file" name="backup_file" accept=".json" required style="font-size: 13px; border: 1px dashed #1095c1; padding: 5px; border-radius: 4px; background: white;">
                        <button type="submit" onclick="return confirm('¿Estás seguro? Esto reemplazará todos los datos de la base de datos actual.')" style="background-color: #e67e22; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 14px; width: auto; margin:0;">
                            📤 Restaurar Datos
                        </button>
                    </form>
                </div>
            </div>
        </section>

    </main>

    <dialog id="profileModal">
        <article>
            <header>
                <a href="#close" aria-label="Close" class="close" onclick="closeProfile()"></a>
                <h3 id="profName">Jugador</h3>
                <p>Variación de ELO Actual: <strong id="profElo">1200</strong></p>
            </header>
            
            <div style="max-height: 250px; margin-bottom:20px;">
                <canvas id="eloChart"></canvas>
            </div>

            <div class="split-profile">
                <div>
                    <h5>Historial de Partidas</h5>
                    <div style="max-height: 250px; overflow-y: auto;">
                        <table role="grid" style="font-size: 0.85rem;">
                            <thead>
                                <tr><th>Fecha</th><th>Resultado</th><th>Score</th><th>Cambio ELO</th></tr>
                            </thead>
                            <tbody id="profMatchesBody"></tbody>
                        </table>
                    </div>
                </div>
                <div>
                    <h5>Análisis de Compañeros</h5>
                    <label style="font-size: 0.8rem; font-weight: bold;">Socios más frecuentes / Frecuencia:</label>
                    <table role="grid" style="font-size: 0.8rem; margin-bottom:15px;">
                        <thead><tr><th>Compañero</th><th>Partidos</th><th>WinRate</th></tr></thead>
                        <tbody id="profTeammatesFreq"></tbody>
                    </table>

                    <label style="font-size: 0.8rem; font-weight: bold;">Mejores / Peores Socios (% Éxito):</label>
                    <table role="grid" style="font-size: 0.8rem;">
                        <thead><tr><th>Compañero</th><th>Partidos</th><th>WinRate</th></tr></thead>
                        <tbody id="profTeammatesPerformance"></tbody>
                    </table>
                </div>
            </div>
        </article>
    </dialog>

    <dialog id="matchModal">
        <article style="max-width: 650px;">
            <header style="margin-bottom: 15px;">
                <a href="#close" aria-label="Close" class="close" onclick="closeMatchDetail()"></a>
                <h3 style="margin:0;">📋 Ficha Técnica del Partido</h3>
                
                <div style="margin-top: 4px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
                    <span id="matchDetailDate" style="font-size:0.85rem; color:#666; font-weight: 500;"></span>
                    
                    <div style="display: flex; gap: 6px; align-items: center;">
                        <button id="btnShowEdit" class="contrast" style="font-size: 11px; padding: 3px 8px; width: auto; margin: 0; line-height: 1;" onclick="toggleEditForm()">✏️ Editar</button>
                        <form id="deleteForm" action="" method="POST" style="margin:0;" onsubmit="return confirm('¿Seguro que deseas eliminar este partido? Esta acción recalculará todo el historial de ELO.');">
                            <button type="submit" style="background-color: #c62828; border-color: #c62828; color: white; font-size: 11px; padding: 3px 8px; width: auto; margin: 0; line-height: 1;">🗑️ Eliminar</button>
                        </form>
                    </div>
                </div>
            </header>
            
            <div class="volley-court">
                <div class="court-side" style="padding-right: 15px;">
                    <h5 style="color: #d81b60; margin-bottom:2px; font-size: 0.95rem;">Equipo A <span id="matchDetailAvgEloA" style="font-weight:normal; font-size:0.75rem; color:#666;"></span></h5>
                    <div id="matchDetailSunA" style="font-size:0.75rem; font-weight:bold; color:#e65100; min-height:18px; margin-bottom:8px;"></div>
                    <h1 id="matchDetailScoreA" style="font-size: 3.2rem; margin:0 0 10px 0; font-weight:bold; color:#d81b60;">0</h1>
                    <div id="matchDetailPlayersA" style="display:flex; flex-direction:column; gap:6px; width:100%; text-align:center;"></div>
                </div>
                
                <div class="volley-net">
                    <div class="net-label">RED DE VOLEY</div>
                </div>
                
                <div class="court-side" style="padding-left: 15px;">
                    <h5 style="color: #1e88e5; margin-bottom:2px; font-size: 0.95rem;">Equipo B <span id="matchDetailAvgEloB" style="font-weight:normal; font-size:0.75rem; color:#666;"></span></h5>
                    <div id="matchDetailSunB" style="font-size:0.75rem; font-weight:bold; color:#e65100; min-height:18px; margin-bottom:8px;"></div>
                    <h1 id="matchDetailScoreB" style="font-size: 3.2rem; margin:0 0 10px 0; font-weight:bold; color:#1e88e5;">0</h1>
                    <div id="matchDetailPlayersB" style="display:flex; flex-direction:column; gap:6px; width:100%; text-align:center;"></div>
                </div>
            </div>
            
            <footer style="text-align: center; padding: 15px 0 0 0; background:none;">
                <h4 id="matchDetailWinner" style="margin:0; font-weight:bold;"></h4>
            </footer>

            <div id="editMatchSection" style="display: none; margin-top: 15px; border-top: 1px dashed #ccc; padding-top: 15px;">
                <h5>Modificar Puntuación y Sol</h5>
                <form id="editForm" action="" method="POST">
                    <div class="grid">
                        <label>Puntos Equipo A
                            <input type="number" id="edit_score_a" name="score_a" min="0" required>
                        </label>
                        <label>Puntos Equipo B
                            <input type="number" id="edit_score_b" name="score_b" min="0" required>
                        </label>
                    </div>
                    <div class="grid">
                        <label><input type="checkbox" id="edit_sun_against_a" name="sun_against_a" value="A" onchange="toggleEditSun('A')"> ☀️ Sol en contra Eq. A</label>
                        <label><input type="checkbox" id="edit_sun_against_b" name="sun_against_b" value="B" onchange="toggleEditSun('B')"> ☀️ Sol en contra Eq. B</label>
                    </div>
                    <button type="submit" class="secondary" style="margin-top:10px;">Confirmar Cambios y Recalcular</button>
                </form>
            </div>
        </article>
    </dialog>

    <script>
        function showTab(tabId, element) {
            document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
            document.querySelectorAll('nav a').forEach(link => link.classList.remove('active-tab'));
            document.getElementById(tabId).classList.add('active');
            if(element) element.classList.add('active-tab');
        }

        function toggleSun(team) {
            if(team === 'A' && document.getElementById('sun_against_a').checked) {
                document.getElementById('sun_against_b').checked = false;
            } else if(team === 'B' && document.getElementById('sun_against_b').checked) {
                document.getElementById('sun_against_a').checked = false;
            }
        }

        function toggleEditSun(team) {
            if(team === 'A' && document.getElementById('edit_sun_against_a').checked) {
                document.getElementById('edit_sun_against_b').checked = false;
            } else if(team === 'B' && document.getElementById('edit_sun_against_b').checked) {
                document.getElementById('edit_sun_against_a').checked = false;
            }
        }

        function toggleEditForm() {
            const sect = document.getElementById('editMatchSection');
            sect.style.display = sect.style.display === 'none' ? 'block' : 'none';
        }

        function validateTeams() {
            const selectsA = document.getElementsByName('team_a');
            const selectsB = document.getElementsByName('team_b');
            const idsA = Array.from(selectsA).map(s => s.value).filter(v => v !== "");
            const idsB = Array.from(selectsB).map(s => s.value).filter(v => v !== "");
            
            const intersection = idsA.filter(id => idsB.includes(id));
            if (intersection.length > 0) {
                alert("Error: Un jugador no puede estar en ambos equipos al mismo tiempo.");
                return false;
            }
            return true;
        }

        const playersList = [{% for p in players %}{ id: "{{ p.id }}", name: "{{ p.name }}" },{% endfor %}];
        function generatePlayerSelects() {
            const sizeA = document.getElementById('size_a').value;
            const sizeB = document.getElementById('size_b').value;
            let htmlA = '', htmlB = '';
            for(let i=0; i<sizeA; i++) { htmlA += createSelectHTML('team_a', i+1); }
            for(let i=0; i<sizeB; i++) { htmlB += createSelectHTML('team_b', i+1); }
            document.getElementById('team_a_container').innerHTML = htmlA;
            document.getElementById('team_b_container').innerHTML = htmlB;
        }
        function createSelectHTML(teamName, index) {
            let html = `<select name="${teamName}" class="player-select" required><option value="">Selecciona Jugador ${index}...</option>`;
            playersList.forEach(p => { html += `<option value="${p.id}">${p.name}</option>`; });
            return html + `</select>`;
        }
        window.onload = generatePlayerSelects;

        let sortDirections = {};
        function sortTable(colIndex, isNumeric) {
            const table = document.getElementById("leaderboardTable");
            const tbody = table.tBodies[0];
            const rows = Array.from(tbody.rows);
            
            sortDirections[colIndex] = !sortDirections[colIndex];
            const dir = sortDirections[colIndex] ? 1 : -1;

            rows.sort((a, b) => {
                let valA = a.cells[colIndex].innerText.trim();
                let valB = b.cells[colIndex].innerText.trim();
                
                if (colIndex === 1) {
                    valA = a.cells[colIndex].querySelector('.player-link').innerText.trim();
                    valB = b.cells[colIndex].querySelector('.player-link').innerText.trim();
                }

                if (isNumeric) {
                    valA = parseFloat(valA.replace('%', '')) || 0;
                    valB = parseFloat(valB.replace('%', '')) || 0;
                    return (valA - valB) * dir;
                }
                return valA.localeCompare(valB) * dir;
            });

            rows.forEach(row => tbody.appendChild(row));
        }

        function openMatchDetail(matchId) {
            fetch(`/match_detail/${matchId}`)
                .then(res => res.json())
                .then(data => {
                    document.getElementById('matchDetailDate').innerText = `Jugado el: ${data.date}`;
                    document.getElementById('matchDetailScoreA').innerText = data.score_a;
                    document.getElementById('matchDetailScoreB').innerText = data.score_b;
                    
                    document.getElementById('matchDetailAvgEloA').innerText = `(Elo Prom: ${data.avg_elo_a})`;
                    document.getElementById('matchDetailAvgEloB').innerText = `(Elo Prom: ${data.avg_elo_b})`;
                    
                    document.getElementById('deleteForm').action = `/delete_match/${matchId}`;
                    document.getElementById('editForm').action = `/edit_match/${matchId}`;
                    
                    document.getElementById('edit_score_a').value = data.score_a;
                    document.getElementById('edit_score_b').value = data.score_b;
                    document.getElementById('editMatchSection').style.display = 'none';
                    
                    if(data.sun_advantage === 'B') {
                        document.getElementById('matchDetailSunA').innerText = "☀️ Sol en contra";
                        document.getElementById('matchDetailSunB').innerText = "";
                        document.getElementById('edit_sun_against_a').checked = true;
                        document.getElementById('edit_sun_against_b').checked = false;
                    } else if(data.sun_advantage === 'A') {
                        document.getElementById('matchDetailSunA').innerText = "";
                        document.getElementById('matchDetailSunB').innerText = "☀️ Sol en contra";
                        document.getElementById('edit_sun_against_a').checked = false;
                        document.getElementById('edit_sun_against_b').checked = true;
                    } else {
                        document.getElementById('matchDetailSunA').innerText = "";
                        document.getElementById('matchDetailSunB').innerText = "";
                        document.getElementById('edit_sun_against_a').checked = false;
                        document.getElementById('edit_sun_against_b').checked = false;
                    }
                    
                    const winnerText = data.score_a > data.score_b ? "🎉 ¡Ganó el Equipo A! 🎉" : "🎉 ¡Ganó el Equipo B! 🎉";
                    document.getElementById('matchDetailWinner').innerText = winnerText;
                    document.getElementById('matchDetailWinner').style.color = data.score_a > data.score_b ? "#d81b60" : "#1e88e5";
                    
                    const containerA = document.getElementById('matchDetailPlayersA');
                    containerA.innerHTML = '';
                    data.team_a.forEach(p => {
                        const sign = p.elo_change >= 0 ? '+' : '';
                        const color = p.elo_change >= 0 ? '#2e7d32' : '#c62828';
                        containerA.innerHTML += `<div style="background:#fff; padding:4px 8px; border-radius:6px; font-size:0.85rem; border:1px solid #ddd;">
                            <strong>${p.name}</strong> <span style="color:#666; font-size:0.75rem;">(${p.elo_at_match})</span> <span style="color:${color}; font-weight:bold;">(${sign}${p.elo_change})</span>
                        </div>`;
                    });

                    const containerB = document.getElementById('matchDetailPlayersB');
                    containerB.innerHTML = '';
                    data.team_b.forEach(p => {
                        const sign = p.elo_change >= 0 ? '+' : '';
                        const color = p.elo_change >= 0 ? '#2e7d32' : '#c62828';
                        containerB.innerHTML += `<div style="background:#fff; padding:4px 8px; border-radius:6px; font-size:0.85rem; border:1px solid #ddd;">
                            <strong>${p.name}</strong> <span style="color:#666; font-size:0.75rem;">(${p.elo_at_match})</span> <span style="color:${color}; font-weight:bold;">(${sign}${p.elo_change})</span>
                        </div>`;
                    });
                    
                    document.getElementById('matchModal').open = true;
                });
        }
        
        function closeMatchDetail() {
            document.getElementById('matchModal').open = false;
        }

        let myChart = null;
        function openProfile(playerId) {
            fetch(`/player_profile/${playerId}`)
                .then(res => res.json())
                .then(data => {
                    document.getElementById('profName').innerText = `📊 Perfil de ${data.name}`;
                    document.getElementById('profElo').innerText = `${data.elo} ELO`;
                    
                    const matchesBody = document.getElementById('profMatchesBody');
                    matchesBody.innerHTML = '';
                    data.matches.forEach(m => {
                        const tr = document.createElement('tr');
                        tr.className = m.result === "Victoria" ? "win-row" : "loss-row";
                        tr.style.cursor = "pointer";
                        tr.title = "Ver detalle de cancha";
                        tr.onclick = () => openMatchDetail(m.id);
                        
                        const sign = m.change >= 0 ? "+" : "";
                        tr.innerHTML = `<td>${m.date}</td><td><strong>${m.result}</strong></td><td>${m.score}</td><td>${sign}${m.change}</td>`;
                        matchesBody.appendChild(tr);
                    });

                    const mates = data.stats.teammates;
                    const freqBody = document.getElementById('profTeammatesFreq');
                    const perfBody = document.getElementById('profTeammatesPerformance');
                    freqBody.innerHTML = ''; perfBody.innerHTML = '';

                    if(mates.length === 0) {
                        freqBody.innerHTML = '<tr><td colspan="3">Sin partidos en equipo</td></tr>';
                        perfBody.innerHTML = '<tr><td colspan="3">Sin partidos en equipo</td></tr>';
                    } else {
                        const sortedByFreq = [...mates].sort((a,b) => b.played - a.played);
                        sortedByFreq.slice(0, 3).forEach(t => {
                            freqBody.innerHTML += `<tr><td>${t.name}</td><td>${t.played}</td><td>${t.winrate}%</td></tr>`;
                        });
                        
                        const sortedByPerf = [...mates].sort((a,b) => b.winrate - a.winrate);
                        perfBody.innerHTML += `<tr style="color:green;"><td>🥇 ${sortedByPerf[0].name}</td><td>${sortedByPerf[0].played}</td><td>${sortedByPerf[0].winrate}%</td></tr>`;
                        if(sortedByPerf.length > 1) {
                            const worst = sortedByPerf[sortedByPerf.length - 1];
                            perfBody.innerHTML += `<tr style="color:red;"><td>💔 ${worst.name}</td><td>${worst.played}</td><td>${worst.winrate}%</td></tr>`;
                        }
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
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
