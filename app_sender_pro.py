# Frontend Python (Tkinter) para gerenciamento e agendamento de mensagens
# VERSÃO COMPLETA E FINAL COM FUNÇÃO DE LIMPEZA DE CONEXÃO E CRON ATIVADOS

import sqlite3
import requests
import tkinter as tk
from tkinter import scrolledtext, messagebox, simpledialog, ttk
from threading import Thread
from apscheduler.schedulers.background import BackgroundScheduler
import time
import datetime
from tkcalendar import Calendar 
import webbrowser
from urllib.parse import quote, urlparse, parse_qs, urlunparse, quote_plus
from bs4 import BeautifulSoup, NavigableString
import re
import os # NOVO: Necessário para manipular o diretório de credenciais
import shutil # NOVO: Necessário para apagar o diretório de credenciais

# --- Configurações ---
BAILEYS_API_URL = 'http://127.0.0.1:3000'
DB_NAME = 'scheduler_db.sqlite'
TIME_FORMAT = '%Y-%m-%d %H:%M:%S' # Formato interno para o scheduler (manter)
OUTPUT_TIME_FORMAT = '%d/%m/%Y %H:%M' # CORREÇÃO: Formato para o DB e URL (Ex: 22/10/2025 08:32)

# Variáveis globais para a GUI
root = None 
output = None
scheduler = BackgroundScheduler()

# Variáveis globais para os elementos de UI e debounce
session_var = None
group_var = None 
group_combo = None 
group_jids_cache = {} 
status_label = None
message_area = None
url_entry = None
debounce_job_id = None 
is_recurring_var = None 
weekday_times_entry = None
weekday_times_var = None 
weekend_times_entry = None
weekend_times_var = None 
last_sent_time_var = None 
tree_scheduled = None 

# --- 1. Gerenciamento do Banco de Dados SQLite ---

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Criação da tabela de sessões
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            status TEXT
        )
    ''')
    
    # 2. Criação da tabela de agendamentos
    c.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            group_name TEXT,
            jid TEXT,
            message TEXT,
            scheduled_time TEXT,
            sent_status TEXT DEFAULT 'PENDING',
            is_recurring INTEGER DEFAULT 0,
            url_source TEXT,
            last_sent_time TEXT DEFAULT 'N/A',
            weekday_times TEXT,
            weekend_times TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    ''')
    
    conn.commit()
    conn.close()

def get_sessions_from_db():
    """Busca todas as sessões registradas e seu status."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, status FROM sessions")
    sessions = c.fetchall()
    conn.close()
    return sessions

# --- 2. Lógica do Agendador (Scheduler) e Verificação de Status ---

def gui_update_log(message, tag='info'):
    if root:
        root.after(0, _gui_update_log_safe, message, tag)

def _gui_update_log_safe(message, tag):
    output.insert(tk.END, message + '\n', tag)
    output.see(tk.END)


def delete_job_from_scheduler(message_id):
    """Remove o job do agendador ativo (APScheduler)."""
    job_id = f'job_{message_id}'
    job_id_wk = f'job_{message_id}_wk'
    job_id_we = f'job_{message_id}_we'
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        if scheduler.get_job(job_id_wk): # Tenta remover jobs de Recorrência (wk)
            scheduler.remove_job(job_id_wk)
        if scheduler.get_job(job_id_we): # Tenta remover jobs de Recorrência (we)
            scheduler.remove_job(job_id_we)

        gui_update_log(f"[SCHEDULER] Agendamento ID {message_id} removido do APScheduler (jobs Único/CRON).", 'info')
        return True
    except Exception as e:
        gui_update_log(f"[SCHEDULER] Erro ao remover Job {message_id}: {e}", 'error')
        return False

def delete_job_from_db(message_id):
    """Remove o registo do agendamento da base de dados SQLite."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM scheduled_messages WHERE id = ?", (message_id,))
        conn.commit()
        conn.close()
        gui_update_log(f"[DB] Agendamento ID {message_id} removido da base de dados.", 'info')
        return True
    except Exception as e:
        gui_update_log(f"[DB] Erro ao remover Job {message_id} da DB: {e}", 'error')
        return False

def delete_selected_job_gui():
    """Lógica para deletar o job selecionado na Treeview (Chamado pelo botão)."""
    global tree_scheduled
    selected_items = tree_scheduled.selection()
    
    if not selected_items:
        messagebox.showwarning("Aviso", "Por favor, selecione o agendamento que deseja excluir na tabela.")
        return

    item = selected_items[0]
    values = tree_scheduled.item(item, 'values')
    
    try:
        message_id = int(values[0])
    except:
        messagebox.showerror("Erro", "ID do Agendamento não encontrado.")
        return

    if not messagebox.askyesno("Confirmação de Exclusão", f"Tem certeza que deseja EXCLUIR o agendamento ID {message_id}? Esta ação é IRREVERSÍVEL e remove o histórico."):
        return
        
    scheduler_ok = delete_job_from_scheduler(message_id)
    db_ok = delete_job_from_db(message_id)
    
    if scheduler_ok and db_ok:
        tree_scheduled.delete(item)
        gui_update_log(f"Agendamento ID {message_id} excluído com sucesso!", 'success')
    else:
        show_scheduled_messages()


def fetch_and_update_db(message_id, url):
    """Busca o conteúdo da URL e atualiza o campo 'message' no DB."""
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status() 
        html_content = response.text
        markdown_message = html_to_whatsapp_markdown(html_content) 
        
        if not markdown_message.strip():
            markdown_message = f"(AVISO: Conteúdo URL não pôde ser extraído em {datetime.datetime.now().strftime('%H:%M:%S')})"
            
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE scheduled_messages SET message = ? WHERE id = ?", (markdown_message, message_id))
        conn.commit()
        conn.close()
        
        return True
        
    except requests.exceptions.RequestException as e:
        gui_update_log(f"[JOB {message_id}] FALHA na busca da URL {url}: {e}", 'error')
        return False


def send_scheduled_job(message_id):
    """Função executada pelo APScheduler para envio final."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute("SELECT session_id, jid, message, is_recurring FROM scheduled_messages WHERE id=?", (message_id,))
    data = c.fetchone()
    
    status_to_update = 'FAILED'
    
    if data:
        session_id, jid, message, is_recurring = data
        
        payload = {'sessionId': session_id, 'jid': jid, 'message': message}
        try:
            response = requests.post(f'{BAILEYS_API_URL}/send_message', json=payload)
            response.raise_for_status()
            result = response.json()
            
            if result.get('status') == 'queued':
                status_to_update = 'QUEUED_SENT' if not is_recurring else 'PENDING'
                gui_update_log(f"[JOB {message_id}] Mensagem ENFILEIRADA na sessão {session_id} (Recorrente: {bool(is_recurring)}).", 'success')
            else:
                status_to_update = 'FAILED_API'
                gui_update_log(f"[JOB {message_id}] FALHA API: {result.get('message')}.", 'error')
            
        except requests.exceptions.RequestException:
            status_to_update = 'FAILED_NETWORK'
            gui_update_log(f"[JOB {message_id}] FALHA CRÍTICA de rede com o Backend.", 'error')
            
        
        # ATUALIZAÇÃO DA BASE DE DADOS
        if status_to_update == 'QUEUED_SENT':
            # Job Único: Marca como SENT
            c.execute("UPDATE scheduled_messages SET sent_status=? WHERE id=?", ('SENT', message_id))
        
        # Correção: Apenas jobs Únicos devem ter o status alterado para FAILED. 
        # Recorrentes (CRON) devem permanecer 'PENDING' para re-tentativa no próximo horário/restart.
        elif (status_to_update == 'FAILED_API' or status_to_update == 'FAILED_NETWORK') and not is_recurring:
            c.execute("UPDATE scheduled_messages SET sent_status=? WHERE id=?", (status_to_update, message_id))
        
        elif (status_to_update == 'FAILED_API' or status_to_update == 'FAILED_NETWORK') and is_recurring:
             gui_update_log(f"[JOB {message_id}] Falha (API/Rede). Status DB MANTIDO em PENDING para re-tentativa.", 'warning')


        conn.commit()
        root.after(0, show_scheduled_messages)
            
    conn.close()

# --- NOVO: FUNÇÃO DE LIMPEZA DE SESSÃO (MANTENDO AGENDAMENTOS) ---

def reset_session_connection(session_id):
    """
    Remove o registro da sessão do banco de dados e apaga o diretório 
    de credenciais do Baileys, MANTENDO os agendamentos (jobs) associados.
    
    Esta função é usada para resolver problemas como status 'loggedout' ou 'connecting' preso, forçando um novo QR.
    """
    if not session_id or session_id in ["Nenhuma Conectada", "Carregando..."]:
        messagebox.showerror("Erro", "Nenhuma sessão válida selecionada para exclusão.")
        return False
        
    if not messagebox.askyesno("Confirmação", 
                              f"Tem certeza que deseja DELETAR a CONEXÃO (crendenciais) da sessão '{session_id}'? "
                              "Isso removerá o registro do DB e as credenciais, forçando novo QR Code. "
                              "**Todos os agendamentos PENDENTES serão MANTIDOS.**"):
        gui_update_log(f"Limpeza da CONEXÃO da sessão {session_id} cancelada pelo usuário.", 'warning')
        return False
        
    try:
        # 1. REMOVER DO BANCO DE DADOS (DB)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # Apagar a entrada da sessão na tabela sessions (ID é a PK no seu código)
        cursor.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        
        conn.commit()
        conn.close()
        
        # 2. REMOVER O DIRETÓRIO DE CREDENCIAIS (auth_info_ID)
        auth_dir_name = f'auth_info_{session_id}'
        
        if os.path.isdir(auth_dir_name):
            # shutil.rmtree apaga o diretório e todo seu conteúdo
            shutil.rmtree(auth_dir_name)
            gui_update_log(f"Diretório de credenciais '{auth_dir_name}' removido com sucesso.", 'success')
        else:
            gui_update_log(f"Diretório de credenciais '{auth_dir_name}' não encontrado para remoção.", 'warning')
            
        gui_update_log(f"Sessão '{session_id}' CONEXÃO foi limpa. Agendamentos foram MANTIDOS. Use '1. Adicionar/Verificar Sessão' para reconectar!", 'success')
        
        # 3. ATUALIZAR INTERFACE
        root.after(0, update_session_selector)
        root.after(0, show_scheduled_messages) 
        
    except sqlite3.Error as e:
        messagebox.showerror("Erro no Banco de Dados", f"Erro ao limpar conexão no DB: {e}")
        gui_update_log(f"ERRO: Falha ao limpar conexão da sessão {session_id} no DB: {e}", 'error')
    except Exception as e:
        messagebox.showerror("Erro no Arquivo", f"Erro ao deletar diretório de credenciais: {e}")
        gui_update_log(f"ERRO: Falha ao excluir diretório {auth_dir_name}: {e}", 'error')
        
    return True

# --- NOVAS FUNÇÕES PARA VERIFICAÇÃO DE STATUS ---
def check_session_status_and_update_db(session_id):
    """Verifica o status da sessão no backend e atualiza o DB. Requer endpoint /session/state no backend."""
    try:
        # NOTA: Usando POST para verificar status, como definido no backend
        response = requests.post(f'{BAILEYS_API_URL}/session/state', json={'sessionId': session_id}, timeout=10)
        response.raise_for_status()
        result = response.json()
        
        current_state = result.get('state', 'UNKNOWN')

        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE sessions SET status = ? WHERE id = ?", (current_state, session_id))
        conn.commit()
        conn.close()

        root.after(0, update_session_selector) # Atualiza a GUI com o novo status
        return current_state
        
    except requests.exceptions.RequestException:
        # Se o backend Node.js estiver offline, a requisição falhará.
        gui_update_log(f"[VERIFICADOR] Falha ao verificar status de {session_id}. Backend Node.js offline ou erro de rede.", 'error')
        return 'NETWORK_ERROR'
        
    except Exception as e:
        gui_update_log(f"[VERIFICADOR] Erro genérico ao verificar {session_id}: {e}", 'error')
        return 'UNKNOWN_ERROR'

def check_all_sessions_status():
    """Itera por todas as sessões do DB e verifica o status de cada uma em threads separadas."""
    sessions = get_sessions_from_db()
    if not sessions:
        gui_update_log("[VERIFICADOR] Nenhuma sessão encontrada para verificação.", 'info')
        return

    gui_update_log("[VERIFICADOR] Iniciando verificação de status de todas as sessões...", 'info')
    
    # Cria uma thread para cada verificação para evitar bloqueio e acelerar o processo.
    for session_id, _ in sessions:
        Thread(target=check_session_status_and_update_db, args=[session_id]).start()


# FUNÇÃO CORRIGIDA PARA RECUPERAR O TEMPO DE CORRÊNCIA 
def get_last_expected_run_time(weekday_times_str, weekend_times_str, current_dt):
    """
    Calcula o último horário de execução CRON esperado que já passou 
    no dia de hoje (usado para recuperação de envios perdidos).
    Retorna o datetime desse horário.
    """
    is_weekday = current_dt.weekday() < 5 
    times_str = weekday_times_str if is_weekday else weekend_times_str
    
    if not times_str: return None

    times_list = []
    try:
        raw_times = [t.strip() for t in times_str.split(',') if t.strip()]
        for t in raw_times:
            if re.match(r'^(?:[01]\d|2[0-3]):[0-5]\d$', t):
                H, M = map(int, t.split(':'))
                dt_scheduled = current_dt.replace(hour=H, minute=M, second=0, microsecond=0)
                times_list.append(dt_scheduled)
    except Exception as e:
        gui_update_log(f"[CRON RECOVERY] ERRO de parse do horário: {e}", 'error')
        return None

    times_list.sort(reverse=True) 
    current_dt_safe = current_dt - datetime.timedelta(seconds=5) 
    
    for dt_scheduled in times_list:
        if dt_scheduled < current_dt_safe:
            return dt_scheduled

    return None 

# FUNÇÃO MESTRA CORRIGIDA (AGORA COM VERIFICAÇÃO DE STATUS DA SESSÃO)
def daily_refresh_and_send(message_id):
    """
    Função mestra para trabalhos recorrentes (CRON) que injeta o tempo na URL 
    e atualiza o last_sent_time.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT session_id, url_source, last_sent_time, weekday_times, weekend_times FROM scheduled_messages WHERE id=?", (message_id,))
    data = c.fetchone()
    conn.close()
    
    if not data:
        gui_update_log(f"[CRON JOB {message_id}] ERRO: Agendamento não encontrado na DB.", 'error')
        return
        
    session_id, url_base, last_sent_time_db, weekday_times_str, weekend_times_str = data

    # *** NOVA ETAPA CRÍTICA: VERIFICAÇÃO DE STATUS DA SESSÃO ***
    # Buscamos o status atualizado do DB, que é constantemente atualizado pelo check_all_sessions_status
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT status FROM sessions WHERE id = ?", (session_id,))
    session_status = c.fetchone()
    conn.close()

    current_status = session_status[0] if session_status else 'NOT_INIT'
    
    if current_status != 'open':
        gui_update_log(f"[CRON JOB {message_id}] ENVIO IGNORADO: Sessão {session_id} não está aberta. Status: {current_status}", 'warning')
        return # Interrompe a execução e espera pelo próximo horário CRON

    if not url_base:
        gui_update_log(f"[CRON JOB {message_id}] ERRO: URL não configurada para job recorrente.", 'error')
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE scheduled_messages SET sent_status = ? WHERE id = ?", ('FAILED_NO_URL', message_id))
        conn.commit()
        conn.close()
        root.after(0, show_scheduled_messages)
        return

    # Lógica de parametrização de URL (Mantida)
    current_run_dt = datetime.datetime.now()
    current_run_time_str = current_run_dt.strftime(OUTPUT_TIME_FORMAT)
    
    prev_time = ""
    
    if last_sent_time_db == 'N/A':
        last_expected_dt = get_last_expected_run_time(weekday_times_str, weekend_times_str, current_run_dt)
        
        if last_expected_dt:
            prev_time_dt = last_expected_dt + datetime.timedelta(minutes=1)
            prev_time = prev_time_dt.strftime(OUTPUT_TIME_FORMAT)
            gui_update_log(f"[CRON JOB {message_id}] INÍCIO (Recuperação): Usando {prev_time} como PREV_TIME.", 'warning')
        else:
            prev_time = current_run_time_str
            gui_update_log(f"[CRON JOB {message_id}] INÍCIO (First Run/Safe): Usando {prev_time} como PREV_TIME.", 'warning')
        
    else:
        try:
            last_sent_dt = datetime.datetime.strptime(last_sent_time_db, OUTPUT_TIME_FORMAT)
            prev_time_dt = last_sent_dt + datetime.timedelta(minutes=1)
            prev_time = prev_time_dt.strftime(OUTPUT_TIME_FORMAT)
            gui_update_log(f"[CRON JOB {message_id}] RECORRÊNCIA NORMAL: Último envio em {last_sent_time_db}.", 'info')
            
        except ValueError:
            gui_update_log(f"[CRON JOB {message_id}] AVISO: last_sent_time_db corrompido/formato antigo. Usando tempo atual como PREV_TIME.", 'error')
            prev_time = current_run_time_str

    preview_param_raw = f"{prev_time} | {current_run_time_str}"
    
    parsed_url = urlparse(url_base)
    query_params = parse_qs(parsed_url.query)
    encoded_preview_value = quote(preview_param_raw)
    
    new_query_parts = []
    for key, values in query_params.items():
        if key.lower() != 'preview':
            for value in values:
                new_query_parts.append(f"{key}={quote_plus(value)}")
    
    new_query_parts.append(f"preview={encoded_preview_value}")
    new_query = "&".join(new_query_parts)
    modified_url = urlunparse(parsed_url._replace(query=new_query))
    
    gui_update_log(f"[CRON JOB {message_id}] Recarregando conteúdo da URL (Preview Param: {preview_param_raw})", 'info')
    
    if fetch_and_update_db(message_id, modified_url):
        
        # SUCESSO na busca da URL -> Atualiza last_sent_time e TENTA enviar
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE scheduled_messages SET last_sent_time = ? WHERE id = ?", (current_run_time_str, message_id))
        conn.commit()
        conn.close()
        
        send_scheduled_job(message_id)
        
    else:
        # FALHA na busca da URL -> Não envia, e o job permanece PENDING para a próxima tentativa CRON
        gui_update_log(f"[CRON JOB {message_id}] ENVIO IGNORADO: Falha ao recarregar URL. Próxima tentativa no próximo horário.", 'error')
        
    root.after(0, show_scheduled_messages)


def load_scheduled_messages():
    """Carrega todas as mensagens agendadas e adiciona ao APScheduler."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, scheduled_time, is_recurring, weekday_times, weekend_times FROM scheduled_messages WHERE sent_status='PENDING'")
    messages = c.fetchall()
    conn.close()
    
    # --- MELHORIA: Verificação de status a cada 5 minutos ---
    scheduler.add_job(
        check_all_sessions_status, 
        'interval', 
        minutes=5, # Verificação de status mais rápida
        id='session_health_check', 
        replace_existing=True
    )
    gui_update_log("[SCHEDULER] Verificação de status das sessões agendada a cada 5 minutos.", 'info')

    # Lógica para carregar os jobs
    for mid, sch_time, is_recurring, weekday_times_str, weekend_times_str in messages:
        try:
            if is_recurring:
                
                # --- JOB PARA DIAS DE SEMANA (wk) ---
                times_wk = [t.strip() for t in weekday_times_str.split(',') if t.strip()]
                if times_wk:
                    hours_wk = ','.join(sorted(list(set([t.split(':')[0] for t in times_wk]))))
                    minutes_wk = ','.join(sorted(list(set([t.split(':')[1] for t in times_wk]))))
                    
                    scheduler.add_job(
                        daily_refresh_and_send, 
                        'cron', 
                        day_of_week='mon-fri',
                        hour=hours_wk,
                        minute=minutes_wk,
                        args=[mid], 
                        id=f'job_{mid}_wk', 
                        replace_existing=True,
                        misfire_grace_time=None 
                    )
                    gui_update_log(f"[SCHEDULER] Job {mid} CRON (Seg-Sex) configurado para: {weekday_times_str}.", 'success')

                # --- JOB PARA FINS DE SEMANA (we) ---
                times_we = [t.strip() for t in weekend_times_str.split(',') if t.strip()]
                if times_we:
                    hours_we = ','.join(sorted(list(set([t.split(':')[0] for t in times_we]))))
                    minutes_we = ','.join(sorted(list(set([t.split(':')[1] for t in times_we]))))
                    
                    scheduler.add_job(
                        daily_refresh_and_send, 
                        'cron', 
                        day_of_week='sat-sun',
                        hour=hours_we,
                        minute=minutes_we,
                        args=[mid], 
                        id=f'job_{mid}_we', 
                        replace_existing=True,
                        misfire_grace_time=None 
                    )
                    gui_update_log(f"[SCHEDULER] Job {mid} CRON (Sáb-Dom) configurado para: {weekend_times_str}.", 'success')
            
            else:
                 # Agendamento Único (date)
                scheduler.add_job(
                    send_scheduled_job, 
                    'date', 
                    run_date=sch_time, 
                    args=[mid], 
                    id=f'job_{mid}', 
                    replace_existing=True,
                    misfire_grace_time=30
                )
                gui_update_log(f"[SCHEDULER] Job {mid} ÚNICO agendado para {sch_time}.", 'info')
                
        except Exception as e:
            gui_update_log(f"[SCHEDULER] Erro ao carregar Job {mid}: {e}", 'error')

# --- 3. Processamento de HTML/Markdown ---
def html_to_whatsapp_markdown(html_content):
    """Converte um fragmento HTML para uma string formatada em Markdown simples."""
    soup = BeautifulSoup(html_content, 'html.parser')
    target = soup.find('body') or soup 
    markdown_text = []

    for element in target.descendants:
        if isinstance(element, NavigableString):
            text = str(element).strip()
            if not text:
                continue

            parent_name = element.parent.name.lower()
            
            if parent_name in ['b', 'strong']:
                text = f'*{text}*' 
            elif parent_name in ['i', 'em']:
                text = f'_{text}_' 
            elif parent_name == 'a':
                href = element.parent.get('href', '#')
                text = f'{text} ({href})' 
            elif parent_name == 'h1':
                text = f'*{text.upper()}*' + '\n' 
            elif parent_name == 'li':
                text = f' • {text}' 
            
            markdown_text.append(text)

        elif element.name in ['br', 'p', 'h1', 'h2', 'li']:
            if markdown_text and markdown_text[-1] != '\n':
                 markdown_text.append('\n')
            
            if element.name in ['p', 'h1', 'h2']:
                markdown_text.append('\n')
    
    final_text = "".join(markdown_text).strip()
    final_text = '\n'.join([line.strip() for line in final_text.splitlines() if line.strip() or line.strip() == '•' or line.strip() == ''])
    
    return final_text


def fetch_and_fill_message():
    """Busca o HTML do URL, converte para Markdown e preenche a área de mensagem."""
    global message_area, url_entry
    
    url = url_entry.get().strip()
    if not url:
        message_area.delete('1.0', tk.END)
        return
    
    gui_update_log(f"Iniciando busca do conteúdo em: {url}", 'info')
    
    def fetch_thread():
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status() 
            html_content = response.text
            
            markdown_message = html_to_whatsapp_markdown(html_content)
            
            if not markdown_message.strip():
                 root.after(0, lambda: gui_update_log(f"AVISO: Conteúdo extraído estava vazio ou não pôde ser formatado.", 'error'))
                 markdown_message = "(Nenhum conteúdo principal encontrado no HTML)"
            
            root.after(0, lambda: [
                message_area.delete('1.0', tk.END),
                message_area.insert(tk.END, markdown_message),
                gui_update_log("Conteúdo HTML convertido e pronto para envio (Formato Markdown).", 'success')
            ])

        except requests.exceptions.RequestException as e:
            root.after(0, lambda: [
                message_area.delete('1.0', tk.END),
                gui_update_log(f"ERRO ao buscar URL: Verifique a URL e a conexão. {e}", 'error')
            ])
        except Exception as e:
            root.after(0, lambda: gui_update_log(f"ERRO de processamento: {e}", 'error'))

    Thread(target=fetch_thread).start()


def debounce_fetch(event):
    """Função que atrasa a conversão da URL em 1 segundo."""
    global debounce_job_id
    
    if debounce_job_id:
        root.after_cancel(debounce_job_id)

    debounce_job_id = root.after(1000, fetch_and_fill_message)

# --- 4. Funções de Conexão e Seleção de Sessão ---

def start_session_thread(session_id):
    """Função de conexão executada em uma thread separada."""
    gui_update_log(f"Iniciando thread de conexão para {session_id}...", 'info')
    browser_opened = False 

    try:
        response = requests.post(f'{BAILEYS_API_URL}/session/start', json={'sessionId': session_id})
        response.raise_for_status()
        result = response.json()
        
        qr_code = result.get('qrCode')
        state = result.get('state', 'connecting')

        while state != 'open' and state != 'loggedOut' and state != 'failed_auth':
            
            if state == 'qr_required' and qr_code and not browser_opened:
                gui_update_log(f"QR CODE detectado! Abrindo no navegador...", 'fail')
                qr_url = f"https://api.qrserver.com/v1/create-qr-code/?data={quote(qr_code)}&size=300x300"
                webbrowser.open(qr_url)
                browser_opened = True
            
            time.sleep(5)
            
            response = requests.post(f'{BAILEYS_API_URL}/session/start', json={'sessionId': session_id})
            response.raise_for_status()
            result = response.json()
            state = result.get('state', 'connecting')
            qr_code = result.get('qrCode')
            
            if state == 'open':
                break
                
            if state != 'qr_required' and state != 'connecting':
                gui_update_log(f"Status atual da sessão {session_id}: {state}", 'info')
            
            if browser_opened and not qr_code and state != 'open':
                gui_update_log(f"QR Code escaneado. Aguardando a conexão...", 'info')

        # --- Após o loop (Conectado ou Falha) ---
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO sessions (id, status) VALUES (?, ?)", (session_id, state))
        conn.commit()
        conn.close()
        
        if state == 'open':
            gui_update_log(f"Sessão {session_id} CONECTADA com sucesso!", 'success')
        else:
            gui_update_log(f"Falha ao conectar {session_id}. Status final: {state}", 'error')

        root.after(0, update_session_selector) 

    except requests.exceptions.RequestException as e:
        gui_update_log(f"ERRO: Não foi possível conectar ao Backend Node.js. {e}", 'error')

def add_new_session_gui():
    """Abre um modal para selecionar uma sessão existente ou criar uma nova."""
    
    top = tk.Toplevel(root) 
    top.title("Selecionar ou Criar Sessão")
    top.grab_set() 
    
    sessions_in_db = get_sessions_from_db()
    session_ids = [s[0] for s in sessions_in_db]
    session_ids.insert(0, "--- Selecionar Existente ---") 
    
    selected_session_var = tk.StringVar(top)
    new_session_var = tk.StringVar(top)
    selected_session_var.set(session_ids[0])
    
    frame_select = ttk.LabelFrame(top, text="1. Selecionar Sessão Existente")
    frame_select.pack(padx=10, pady=10, fill='x')

    tk.Label(frame_select, text="ID da Sessão:").pack(side='left', padx=5, pady=5)
    
    session_selector = ttk.Combobox(frame_select, textvariable=selected_session_var, values=session_ids)
    session_selector.pack(side='left', padx=5, pady=5, fill='x', expand=True)

    def select_and_connect():
        session_id = selected_session_var.get()
        if session_id == "--- Selecionar Existente ---":
            messagebox.showwarning("Aviso", "Por favor, selecione um ID de sessão válido.")
            return
        
        top.destroy()
        Thread(target=start_session_thread, args=[session_id]).start()

    ttk.Button(frame_select, text="Conectar/Verificar Status", command=select_and_connect).pack(side='right', padx=5, pady=5)


    frame_new = ttk.LabelFrame(top, text="2. Criar Nova Sessão")
    frame_new.pack(padx=10, pady=10, fill='x')
    
    tk.Label(frame_new, text="Novo ID Único:").pack(side='left', padx=5, pady=5)
    
    new_entry = tk.Entry(frame_new, textvariable=new_session_var)
    new_entry.pack(side='left', padx=5, pady=5, fill='x', expand=True)
    
    def create_and_connect():
        session_id = new_session_var.get().strip()
        if not session_id:
            messagebox.showwarning("Aviso", "O campo do novo ID não pode ser vazio.")
            return
        if session_id in session_ids:
            messagebox.showwarning("Aviso", f"O ID '{session_id}' já existe. Por favor, selecione-o na lista acima.")
            return
        
        top.destroy()
        Thread(target=start_session_thread, args=[session_id]).start()
    
    ttk.Button(frame_new, text="Criar Nova e Conectar", command=create_and_connect).pack(side='right', padx=5, pady=5)


# --- 5. Funções de Atualização e Agendamento ---

def update_group_selector():
    """Busca o cache de JIDs do backend e atualiza o ComboBox de grupos."""
    global group_jids_cache, group_combo, group_var 

    try:
        response = requests.get(f'{BAILEYS_API_URL}/group_jids')
        response.raise_for_status()
        group_jids_cache = response.json()
        
        group_names = sorted([name for name in group_jids_cache.keys()])
        
        if group_names:
            group_combo['values'] = group_names 
            if group_var.get() not in group_names:
                group_var.set(group_names[0])
        else:
            group_var.set("Nenhum Grupo Capturado")
            group_combo['values'] = []
        
    except requests.exceptions.RequestException as e:
        gui_update_log(f"AVISO: Não foi possível buscar o cache de grupos do Backend. ({e})", 'error')
        group_var.set("Nenhum Grupo Capturado")
        group_combo['values'] = []


def update_session_selector():
    """Atualiza a lista de sessões no dropdown e o status na tela."""
    global session_var, status_label, frame_inputs 
    
    session_data = get_sessions_from_db()
    available_sessions = [s[0] for s in session_data if s[1] == 'open']
    
    session_options = available_sessions if available_sessions else ["Nenhuma Conectada"]
    current_selection = session_var.get()
    
    try:
        # Tenta pegar o OptionMenu atual para substituí-lo
        session_menu_widget = frame_inputs.nametowidget(frame_inputs.winfo_children()[2]) 
        if isinstance(session_menu_widget, tk.OptionMenu):
             session_menu_widget.destroy()
    except Exception:
        pass
    
    # Criar e posicionar o novo OptionMenu
    new_session_menu = tk.OptionMenu(frame_inputs, session_var, *session_options)
    new_session_menu.grid(row=0, column=1, padx=5, pady=5, sticky='ew')
    
    if current_selection in available_sessions:
        session_var.set(current_selection)
    elif available_sessions:
        session_var.set(available_sessions[0])
    else:
        session_var.set("Nenhuma Conectada")
        
    status_text = "Contas Conectadas:\n" + "\n".join([f"  {sid}: {status.upper()}" for sid, status in session_data])
    status_label.config(text=status_text)
    
    if available_sessions:
        update_group_selector()


def open_calendar_and_time():
    """Abre o seletor de calendário e hora para AGENDAMENTO ÚNICO."""
    top = tk.Toplevel(root) 
    top.title("Selecionar Data e Hora (Agendamento Único)")
    
    cal = Calendar(top, selectmode='day', date_pattern='yyyy-mm-dd')
    cal.pack(padx=10, pady=10)
    
    tk.Label(top, text="Hora (HH:MM):").pack(padx=10, pady=5)
    time_entry = tk.Entry(top)
    time_entry.insert(0, datetime.datetime.now().strftime("%H:%M")) 
    time_entry.pack(padx=10, pady=5)
    
    def on_date_select():
        try:
            date_str = cal.get_date()
            time_str_raw = time_entry.get()
            
            if len(time_str_raw) != 5 or time_str_raw[2] != ':':
                messagebox.showerror("Erro de Formato", "A hora deve estar no formato HH:MM (ex: 14:30).")
                return

            full_time_str = f"{date_str} {time_str_raw}:00"
            scheduled_datetime = datetime.datetime.strptime(full_time_str, TIME_FORMAT)
            
            if scheduled_datetime <= datetime.datetime.now():
                messagebox.showerror("Erro", "A data/hora deve ser no futuro.")
                return

            schedule_message_gui(scheduled_datetime.strftime(TIME_FORMAT), is_recurring=0, weekday_times_str="", weekend_times_str="")
            top.destroy()
            
        except ValueError as e:
            messagebox.showerror("Erro", f"Erro ao processar data/hora. Verifique se a hora está correta: {e}")

    tk.Button(top, text="Confirmar Horário Único", command=on_date_select).pack(pady=10)


def validate_daily_times(times_str):
    """Valida se a string de horários está no formato HH:MM,HH:MM,..."""
    times = [t.strip() for t in times_str.split(',') if t.strip()]
    
    if not times:
        return True, [] 
        
    time_pattern = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$') 

    for t in times:
        if not time_pattern.match(t):
            return False, f"O formato '{t}' é inválido. Use HH:MM."

    return True, times


def schedule_message_gui(scheduled_time=None, is_recurring=None, weekday_times_str=None, weekend_times_str=None):
    """Lógica de agendamento principal."""
    global is_recurring_var
    
    session_id = session_var.get()
    url_source = url_entry.get().strip()
    
    is_recurring_check = is_recurring_var.get()
    
    if session_id in ["Nenhuma Conectada", "Carregando..."]:
        messagebox.showerror("Erro", "Selecione uma sessão conectada para envio.")
        return
    group_input = group_var.get()
    if not group_input or group_input == "Nenhum Grupo Capturado": 
        messagebox.showerror("Erro", "O campo Destino é obrigatório.")
        return
    message = message_area.get('1.0', tk.END).strip()
    
    if is_recurring_check:
        
        weekday_times_input = weekday_times_var.get().strip()
        weekend_times_input = weekend_times_var.get().strip()
        
        is_valid_wk, validation_wk_result = validate_daily_times(weekday_times_input)
        if not is_valid_wk:
             messagebox.showerror("Erro de Recorrência", f"Horários de Seg-Sex inválidos: {validation_wk_result}")
             return
        
        is_valid_we, validation_we_result = validate_daily_times(weekend_times_input)
        if not is_valid_we:
             messagebox.showerror("Erro de Recorrência", f"Horários de Sáb-Dom inválidos: {validation_we_result}")
             return

        if not weekday_times_input and not weekend_times_input:
            messagebox.showerror("Erro de Recorrência", "Pelo menos um dos campos de Horários (Seg-Sex ou Sáb-Dom) deve ser preenchido.")
            return

        if not url_source:
            messagebox.showerror("Erro de Recorrência", "Agendamentos RECORRENTES devem ter uma URL de Conteúdo para garantir a atualização dinâmica.")
            return

        is_recurring = 1
        weekday_times_str = weekday_times_input
        weekend_times_str = weekend_times_input
        
        # O scheduled_time de um job CRON é apenas um valor 'placeholder' no DB (não usado pelo scheduler)
        all_times = weekday_times_input.split(',') + weekend_times_input.split(',')
        first_time = next((t.strip() for t in all_times if t.strip()), "00:00")
        scheduled_time = datetime.datetime.now().strftime('%Y-%m-%d') + f" {first_time}:00"
        
    else:
        if not message and not url_source:
            messagebox.showerror("Erro", "O campo Mensagem ou URL é obrigatório.")
            return
            
        if scheduled_time is None:
            open_calendar_and_time()
            return

    jid = group_input
    group_name_for_db = group_input
    
    if group_input.lower() in group_jids_cache:
        jid = group_jids_cache[group_input.lower()]
        group_name_for_db = group_input.lower()
    elif group_input.isdigit() and len(group_input) >= 11:
        jid = f"{group_input}@s.whatsapp.net"
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO scheduled_messages (session_id, group_name, jid, message, scheduled_time, is_recurring, url_source, weekday_times, weekend_times, last_sent_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
              (session_id, group_name_for_db, jid, message, scheduled_time, is_recurring, url_source, weekday_times_str, weekend_times_str, 'N/A'))
    message_id = c.lastrowid
    conn.commit()
    conn.close()

    if is_recurring:
        
        if weekday_times_str:
            times_wk = [t.strip() for t in weekday_times_str.split(',') if t.strip()]
            hours_wk = ','.join(sorted(list(set([t.split(':')[0] for t in times_wk]))))
            minutes_wk = ','.join(sorted(list(set([t.split(':')[1] for t in times_wk]))))
            
            scheduler.add_job(daily_refresh_and_send, 'cron', 
                              day_of_week='mon-fri', hour=hours_wk, minute=minutes_wk, 
                              args=[message_id], id=f'job_{message_id}_wk', 
                              replace_existing=True, misfire_grace_time=None)
        
        if weekend_times_str:
            times_we = [t.strip() for t in weekend_times_str.split(',') if t.strip()]
            hours_we = ','.join(sorted(list(set([t.split(':')[0] for t in times_we]))))
            minutes_we = ','.join(sorted(list(set([t.split(':')[1] for t in times_we]))))
            
            scheduler.add_job(daily_refresh_and_send, 'cron', 
                              day_of_week='sat-sun', hour=hours_we, minute=minutes_we, 
                              args=[message_id], id=f'job_{message_id}_we', 
                              replace_existing=True, misfire_grace_time=None)

        log_msg = f"MENSAGEM CRON AGENDADA: ID {message_id}. Recorrência Seg-Sex: {weekday_times_str}, Sáb-Dom: {weekend_times_str}."
        
    else:
        scheduler.add_job(
            send_scheduled_job, 
            'date', 
            run_date=scheduled_time, 
            args=[message_id], 
            id=f'job_{message_id}', 
            replace_existing=True,
            misfire_grace_time=30 
        )
        log_msg = f"MENSAGEM ÚNICA AGENDADA: ID {message_id} para {scheduled_time}."


    gui_update_log(log_msg, 'success')
    root.after(0, show_scheduled_messages)


# --- FUNÇÃO PARA EXIBIR MENSAGENS AGENDADAS ---

def show_scheduled_messages():
    """Busca todas as mensagens agendadas e preenche a Treeview."""
    global tree_scheduled 
    
    if not tree_scheduled:
        return 
        
    for row in tree_scheduled.get_children():
        tree_scheduled.delete(row)
        
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, session_id, group_name, scheduled_time, sent_status, is_recurring, weekday_times, weekend_times FROM scheduled_messages ORDER BY id DESC")
    messages = c.fetchall()
    conn.close()
    
    status_map = {
        'PENDING': ('AGENDADA', 'blue'),
        'SENT': ('ENVIADA (Única)', 'green'),
        'FAILED_NETWORK': ('ERRO: Rede Backend (Única)', 'red'),
        'FAILED_API': ('ERRO: Sessão Fechada/API (Única)', 'red'),
        'FAILED_FETCH': ('ERRO: Falha Busca URL (Única)', 'darkred'),
        'FAILED_NO_URL': ('ERRO: URL Ausente (CRON)', 'darkred'),
    }
    
    for mid, session_id, group_name, sch_time, status_db, is_recurring, weekday_times_str, weekend_times_str in messages:
        
        display_status, color_tag = status_map.get(status_db, ('DESCONHECIDO', 'gray'))

        if is_recurring:
            wk = f"S-S: {weekday_times_str}" if weekday_times_str else ""
            we = f"S-D: {weekend_times_str}" if weekend_times_str else ""
            sch_display = f"Recorrente ({' | '.join(filter(None, [wk, we]))})"
            
            # Se for recorrente, o status real deve ser sempre 'PENDING' na DB para persistência
            if status_db == 'PENDING':
                display_status = "RECORRENTE (CRON)"
            else: 
                # Se o job recorrente tem status de falha (e.g. FAILED_NO_URL), mostramos a falha.
                pass 
                
        else:
            sch_display = sch_time

        tree_scheduled.insert(
            '', 
            'end', 
            values=(mid, session_id, group_name.upper(), sch_display, display_status),
            tags=(color_tag,)
        )
        
    tree_scheduled.tag_configure('blue', foreground='blue')
    tree_scheduled.tag_configure('green', foreground='green')
    tree_scheduled.tag_configure('red', foreground='red', font=('TkDefaultFont', 10, 'bold'))
    tree_scheduled.tag_configure('darkred', foreground='darkred', font=('TkDefaultFont', 10, 'bold'))
    tree_scheduled.tag_configure('gray', foreground='gray')


# --- Inicialização da Aplicação ---

if __name__ == "__main__":
    init_db() 

    root = tk.Tk()
    root.title("Multi-WhatsApp Sender PRO (Python/Baileys)")
    root.geometry("1100x850") 

    notebook = ttk.Notebook(root)
    notebook.grid(row=0, column=0, columnspan=2, padx=10, pady=10, sticky="nsew")
    
    frame_send = ttk.Frame(notebook, padding="10")
    notebook.add(frame_send, text='  Agendar Envio  ')

    frame_status = ttk.Frame(notebook, padding="10")
    notebook.add(frame_status, text='  Status/Histórico  ')
    
    frame_send.grid_rowconfigure(0, weight=1)
    frame_send.grid_rowconfigure(1, weight=0)
    frame_send.grid_rowconfigure(2, weight=0)
    frame_send.grid_rowconfigure(3, weight=0)
    frame_send.grid_columnconfigure(0, weight=1)
    frame_send.grid_columnconfigure(1, weight=1)

    output = scrolledtext.ScrolledText(frame_send, wrap=tk.WORD, state='normal', height=8)
    output.grid(row=0, column=0, columnspan=2, padx=0, pady=0, sticky="nsew")
    
    output.tag_config('success', foreground='green', font=('TkDefaultFont', 10, 'bold'))
    output.tag_config('fail', foreground='red', font=('TkDefaultFont', 10, 'bold'))
    output.tag_config('error', foreground='darkred', font=('TkDefaultFont', 10, 'bold'))
    output.tag_config('info', foreground='blue')

    frame_inputs = tk.Frame(frame_send)
    frame_inputs.grid(row=1, column=0, columnspan=2, padx=0, pady=5, sticky="ew")

    frame_inputs.grid_columnconfigure(1, weight=1) 
    frame_inputs.grid_columnconfigure(2, weight=1) 

    tk.Label(frame_inputs, text="Conta de Envio:").grid(row=0, column=0, padx=5, pady=5, sticky='w')
    session_var = tk.StringVar(frame_inputs, value="Carregando...")
    tk.OptionMenu(frame_inputs, session_var, "Carregando...").grid(row=0, column=1, padx=5, pady=5, sticky='ew')
    
    tk.Label(frame_inputs, text="Destino (Grupo/Contato):").grid(row=1, column=0, padx=5, pady=5, sticky='w')
    group_var = tk.StringVar(frame_inputs, value="Nenhum Grupo Capturado")
    group_combo = ttk.Combobox(frame_inputs, textvariable=group_var, values=[], postcommand=update_group_selector)
    group_combo.grid(row=1, column=1, padx=5, pady=5, sticky='ew')
    
    status_label = tk.Label(frame_inputs, text="Contas Conectadas:\n(Carregando...)", justify=tk.LEFT, borderwidth=1, relief="solid")
    status_label.grid(row=0, column=2, rowspan=2, padx=10, pady=5, sticky='nsew')
    
    tk.Label(frame_inputs, text="URL do Conteúdo:").grid(row=3, column=0, padx=5, pady=5, sticky='w')
    url_entry = tk.Entry(frame_inputs)
    url_entry.insert(0, "")
    url_entry.grid(row=3, column=1, padx=5, pady=5, sticky='ew')
    
    url_entry.bind('<KeyRelease>', debounce_fetch)
    
    btn_fetch = tk.Button(frame_inputs, text="Buscar Agora (Preview)", command=fetch_and_fill_message, bg='#FFE0B2')
    btn_fetch.grid(row=3, column=2, padx=10, pady=5, sticky='ew')
    
    tk.Label(frame_inputs, text="Mensagem (Preenchida por URL ou Manual):").grid(row=4, column=0, padx=5, pady=5, sticky='nw')
    message_area = scrolledtext.ScrolledText(frame_inputs, wrap=tk.WORD, height=8) 
    message_area.grid(row=4, column=1, columnspan=2, padx=5, pady=5, sticky="ew")

    frame_recurrence = tk.Frame(frame_send)
    frame_recurrence.grid(row=2, column=0, columnspan=2, padx=0, pady=(0, 5), sticky="ew")
    frame_recurrence.grid_columnconfigure(1, weight=1)
    frame_recurrence.grid_columnconfigure(3, weight=1)

    is_recurring_var = tk.IntVar()
    tk.Checkbutton(frame_recurrence, 
                   text="3. Agendar Múltiplas Vezes Diariamente (CRON)", 
                   variable=is_recurring_var).grid(row=0, column=0, columnspan=4, padx=5, pady=5, sticky='w')
    
    tk.Label(frame_recurrence, text="Horários Seg-Sex (HH:MM,...):").grid(row=1, column=0, padx=5, pady=5, sticky='w')
    weekday_times_var = tk.StringVar(frame_recurrence)
    weekday_times_entry = tk.Entry(frame_recurrence, textvariable=weekday_times_var)
    weekday_times_entry.grid(row=1, column=1, padx=5, pady=5, sticky='ew')
    
    tk.Label(frame_recurrence, text="Horários Sáb-Dom (HH:MM,...):").grid(row=1, column=2, padx=10, pady=5, sticky='w')
    weekend_times_var = tk.StringVar(frame_recurrence)
    weekend_times_entry = tk.Entry(frame_recurrence, textvariable=weekend_times_var)
    weekend_times_entry.grid(row=1, column=3, padx=5, pady=5, sticky='ew')


    frame_buttons = tk.Frame(frame_send)
    frame_buttons.grid(row=3, column=0, columnspan=2, pady=5, sticky="ew")

    btn_add_session = tk.Button(frame_buttons, 
                                text="1. Adicionar/Verificar Sessão", 
                                command=add_new_session_gui, 
                                bg='#BBDEFB')
    btn_add_session.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

    btn_refresh_status = tk.Button(frame_buttons, 
                                text="Verificar Status AGORA", 
                                command=lambda: Thread(target=check_all_sessions_status).start(), 
                                bg='#FFFFE0') # Amarelo claro
    btn_refresh_status.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

    # ⬅️ BOTÃO DE LIMPEZA DE CONEXÃO AGORA DEVE APARECER ⬅️
    btn_reset_connection = tk.Button(frame_buttons, 
                                   text="LIMPAR CONEXÃO (Novo QR)", 
                                   command=lambda: reset_session_connection(session_var.get()), 
                                   bg='#FF9900', fg='white') 
    btn_reset_connection.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X) 
    
    btn_schedule = tk.Button(frame_buttons, 
                             text="2. Agendar Mensagem (Única ou Múltipla)", 
                             command=lambda: schedule_message_gui(), 
                             bg='#C8E6C9')
    btn_schedule.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

    frame_status.grid_columnconfigure(0, weight=1)
    frame_status.grid_rowconfigure(0, weight=1)
    
    columns = ('id', 'session', 'group', 'scheduled_time', 'status')
    tree_scheduled = ttk.Treeview(frame_status, columns=columns, show='headings')
    
    tree_scheduled.heading('id', text='ID', anchor='center')
    tree_scheduled.heading('session', text='Conta', anchor='center')
    tree_scheduled.heading('group', text='Destino', anchor='w')
    tree_scheduled.heading('scheduled_time', text='Hora Agendada/CRON', anchor='w')
    tree_scheduled.heading('status', text='Status', anchor='center')
    
    tree_scheduled.column('id', width=50, stretch=tk.NO, anchor='center')
    tree_scheduled.column('session', width=100, stretch=tk.NO, anchor='center')
    tree_scheduled.column('group', width=200, stretch=tk.YES, anchor='w')
    tree_scheduled.column('scheduled_time', width=250, stretch=tk.YES, anchor='w')
    tree_scheduled.column('status', width=150, stretch=tk.NO, anchor='center')
    
    tree_scheduled.grid(row=0, column=0, sticky='nsew', padx=5, pady=5)
    
    scrollbar = ttk.Scrollbar(frame_status, orient=tk.VERTICAL, command=tree_scheduled.yview)
    tree_scheduled.configure(yscrollcommand=scrollbar.set)
    scrollbar.grid(row=0, column=1, sticky='ns')

    btn_delete = tk.Button(frame_status, 
                           text="Excluir Agendamento Selecionado", 
                           command=delete_selected_job_gui, 
                           bg='#F4CCCC')
    btn_delete.grid(row=1, column=0, columnspan=2, pady=(10, 5))
    
    btn_refresh = tk.Button(frame_status, 
                            text="Atualizar Lista Manualmente", 
                            command=show_scheduled_messages, 
                            bg='#F0F0F0')
    btn_refresh.grid(row=2, column=0, columnspan=2, pady=5)


    # --- INICIALIZAÇÃO FINAL ---
    scheduler.start()
    gui_update_log("APScheduler iniciado. Carregando jobs pendentes...", 'info')
    
    load_scheduled_messages() 
    update_session_selector() 
    show_scheduled_messages() 
    
    # NOVO: Verificação inicial do status de todas as sessões ao iniciar
    gui_update_log("[INICIALIZAÇÃO] Iniciando verificação inicial de status de todas as contas.", 'info')
    Thread(target=check_all_sessions_status).start()

    root.grid_rowconfigure(0, weight=1)
    root.grid_columnconfigure(0, weight=1)
    root.grid_columnconfigure(1, weight=1)

    def on_closing():
        if messagebox.askokcancel("Sair", "Tem certeza que deseja fechar? O agendador será parado."):
            scheduler.shutdown()
            root.destroy()
    
    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()