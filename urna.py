import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import random
from datetime import datetime, timedelta, timezone

from flask import Flask, g, jsonify, request

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Sao_Paulo")
except ImportError:
    TZ = timezone(timedelta(hours=-3))

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
DB_ARQUIVO = os.environ.get("URNA_DB", "urna.db")
CHAVE_ARQUIVO = os.environ.get("URNA_CHAVE", "chave_hmac.txt")
SALT_ARQUIVO = os.environ.get("URNA_SALT", "salt_eleicao.txt")
TOKEN_VALIDADE = 600

# ---------------------------------------------------------------------------
# JANELA DE VOTAÇÃO (Brasília)
# ---------------------------------------------------------------------------
ABRE_DT = datetime(2026, 10, 9, 10, 39, 0, tzinfo=TZ)
FECHA_DT = datetime(2026, 10, 9, 10, 50, 0, tzinfo=TZ)

MIN_JITTER = 1.0
MAX_JITTER = 4.0

_lock_voto = threading.Lock()

# ---------------------------------------------------------------------------
# Candidatos
# ---------------------------------------------------------------------------
MODO_TESTE_RAW = os.environ["MODO_TESTE"].lower()

if MODO_TESTE_RAW not in ("true", "false"):
    raise RuntimeError(
        "MODO_TESTE deve ser obrigatoriamente 'true' ou 'false'."
    )

MODO_TESTE = MODO_TESTE_RAW == "true"

CANDIDATOS_REAIS = {
    "31": {"nome": "André", "partido": "Revolução Integralista Brasileiro"},
    "67": {"nome": "Marcelinho + Gulin", "partido": "Partido Liberal Games"},
    "22": {"nome": "Vitinho", "partido": "Partido Comunista Revolucionário"}
}

CANDIDATOS_TESTE = {
    "11": {"nome": "Joãozinho", "partido": "Partido Nacional Popular"},
    "14": {"nome": "Zeca", "partido": "Movimento Democrático Nacional"},
    "27": {"nome": "Bruninho", "partido": "Partido da Renovação Brasileira"},
    "30": {"nome": "Pedrão", "partido": "Aliança Progressista"},
    "35": {"nome": "Juninho", "partido": "Partido Trabalhista Nacional"},
    "41": {"nome": "Dudu", "partido": "Frente Liberal Democrática"},
    "48": {"nome": "Rafa", "partido": "Partido da Ordem e Progresso"},
    "52": {"nome": "Gui", "partido": "Movimento Popular Brasileiro"},
    "69": {"nome": "Biel", "partido": "Partido da Nova Geração"},
    "75": {"nome": "Nando", "partido": "União Nacional Independente"},
}

CANDIDATOS = CANDIDATOS_TESTE if MODO_TESTE else CANDIDATOS_REAIS

# ---------------------------------------------------------------------------
# Títulos (16 — determinísticos)
# ---------------------------------------------------------------------------
_rng = random.Random(2026)
TITULOS_SEMEADOS = sorted({_rng.randint(100000000000, 999999999999)
                           for _ in range(16)})


# ---------------------------------------------------------------------------
# Janela de votação
# ---------------------------------------------------------------------------
def agora_brasilia() -> datetime:
    return datetime.now(TZ)


def eleicao_aberta() -> bool:
    n = agora_brasilia()
    return ABRE_DT <= n < FECHA_DT


def fase() -> str:
    n = agora_brasilia()
    if n < ABRE_DT:
        return "antes"
    if n < FECHA_DT:
        return "durante"
    return "depois"


def str_janela(d: datetime) -> str:
    return d.strftime("%d/%m/%Y às %H:%M")


def _bloqueio_apuracao():
    if fase() == "durante":
        return jsonify({
            "erro": "Votação em andamento.",
            "detalhe": "Apuração e verificação de integridade "
                       "ficam bloqueadas durante a votação e "
                       "são liberadas após o encerramento "
                       f"({str_janela(FECHA_DT)}, Brasília)."
        }), 403
    return None


def _erro_janela():
    if fase() == "antes":
        return jsonify({
            "erro": "Eleição ainda não iniciada.",
            "detalhe": f"Abre em {str_janela(ABRE_DT)}."
        }), 403
    return jsonify({
        "erro": "Eleição encerrada.",
        "detalhe": f"Votação encerrada em "
                   f"{str_janela(FECHA_DT)} (Brasília)."
    }), 403


def segundos_para_abrir() -> int:
    n = agora_brasilia()
    if n >= ABRE_DT:
        return 0
    return max(0, int((ABRE_DT - n).total_seconds()))


def segundos_para_fechar() -> int:
    n = agora_brasilia()
    if n >= FECHA_DT:
        return 0
    return max(0, int((FECHA_DT - n).total_seconds()))


def info_janela() -> dict:
    aberta = eleicao_aberta()
    return {
        "aberta": aberta,
        "abre_ts": int(ABRE_DT.timestamp()),
        "fecha_ts": int(FECHA_DT.timestamp()),
        "agora_ts": int(agora_brasilia().timestamp()),
        "falta_segundos": segundos_para_abrir(),
        "abre": ABRE_DT.strftime("%H:%M"),
        "fecha": FECHA_DT.strftime("%H:%M"),
        "data": ABRE_DT.strftime("%d/%m/%Y"),
        "abre_full": str_janela(ABRE_DT),
        "fecha_full": str_janela(FECHA_DT),
    }


# ---------------------------------------------------------------------------
# Hash de títulos
# ---------------------------------------------------------------------------
def _garantir_salt() -> bytes:
    if not os.path.exists(SALT_ARQUIVO):
        with open(SALT_ARQUIVO, "wb") as f:
            f.write(secrets.token_bytes(64))
    with open(SALT_ARQUIVO, "rb") as f:
        return f.read()


def hash_titulo(titulo: str, salt: bytes) -> str:
    return hashlib.sha256(salt + titulo.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Banco de dados — PERSISTENTE
# ---------------------------------------------------------------------------
app = Flask(__name__)

app.config["PREFERRED_URL_SCHEME"] = "https"
if os.environ.get("TRUST_PROXY", "1") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_ARQUIVO)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_ARQUIVO)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS eleitores (
            titulo_hash TEXT PRIMARY KEY,
            votou       INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS votos (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            voto      TEXT NOT NULL,
            hash      TEXT NOT NULL,
            prev_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pool_titulos (
            titulo_hash TEXT PRIMARY KEY,
            titulo      TEXT NOT NULL UNIQUE,
            atribuido   INTEGER NOT NULL DEFAULT 0
        );
    """)
    db.commit()

    salt = _garantir_salt()

    if db.execute("SELECT COUNT(*) FROM eleitores").fetchone()[0] == 0:
        for titulo in TITULOS_SEMEADOS:
            db.execute(
                "INSERT INTO eleitores (titulo_hash, votou) VALUES (?, 0)",
                (hash_titulo(str(titulo), salt),))
        db.commit()

    if db.execute("SELECT COUNT(*) FROM pool_titulos").fetchone()[0] == 0:
        for titulo in TITULOS_SEMEADOS:
            db.execute(
                "INSERT OR IGNORE INTO pool_titulos "
                "(titulo_hash, titulo, atribuido) VALUES (?, ?, 0)",
                (hash_titulo(str(titulo), salt), str(titulo)))
        db.commit()

    db.close()

    if not os.path.exists(CHAVE_ARQUIVO):
        with open(CHAVE_ARQUIVO, "w") as f:
            f.write(secrets.token_hex(32))


def carregar_chave() -> bytes:
    with open(CHAVE_ARQUIVO) as f:
        return f.read().strip().encode()


# ---------------------------------------------------------------------------
# Cadeia de integridade (HMAC)
# ---------------------------------------------------------------------------
def calcular_hash(chave: bytes, voto: str, prev_hash: str) -> str:
    return hmac.new(chave, (voto + prev_hash).encode(),
                    hashlib.sha256).hexdigest()


def verificar_integridade(db) -> dict:
    chave = carregar_chave()
    linhas = db.execute(
        "SELECT id, voto, hash, prev_hash FROM votos ORDER BY id"
    ).fetchall()
    prev = "0" * 64
    for linha in linhas:
        esperado = calcular_hash(chave, linha["voto"],
                                 linha["prev_hash"])
        if (linha["prev_hash"] != prev
                or linha["hash"] != esperado):
            return {
                "integridade": "ERRO",
                "detalhe": (f"O registro {linha['id']} não "
                            f"corresponde ao hash esperado.")
            }
        prev = linha["hash"]
    return {
        "integridade": "OK",
        "detalhe": (f"{len(linhas)} registros verificados. "
                    f"Nenhuma alteração detectada.")
    }


# ---------------------------------------------------------------------------
# Sessões temporárias (RAM)
# ---------------------------------------------------------------------------
sessoes_temporarias: dict = {}


def _limpar_expiradas():
    agora = time.time()
    for t in [t for t, c in sessoes_temporarias.items()
              if agora - c["ts"] > TOKEN_VALIDADE]:
        sessoes_temporarias.pop(t, None)


def _jitter_decorrido(token: str) -> bool:
    sess = sessoes_temporarias.get(token)
    if sess is None:
        return False
    decorrido = time.time() - sess["ts"]
    limite = MIN_JITTER + random.uniform(
        0, MAX_JITTER - MIN_JITTER)
    return decorrido >= limite


# ---------------------------------------------------------------------------
# HTML — Tela principal (contador + login + urna)
# ---------------------------------------------------------------------------
HTML_PRINCIPAL = r"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Urna Eletrônica</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#006400;font-family:Arial,sans-serif;
     display:flex;justify-content:center;min-height:100vh;padding:20px}
.urna{background:#004d00;border:3px solid #003300;border-radius:16px;
      padding:24px;width:420px;box-shadow:0 8px 32px rgba(0,0,0,.5)}
.topo{background:#006b00;border-radius:8px;padding:10px 14px;
      display:flex;justify-content:space-between;align-items:center;
      margin-bottom:14px;border:1px solid #008c00}
.topo-titulo{color:#fff;font-size:13px;font-weight:bold;
             letter-spacing:2px}
.topo-brasil{color:#ffdf00;font-size:11px;font-weight:bold}
.display{background:#0a1628;border:2px solid #1a3a5c;
         border-radius:8px;padding:18px;margin-bottom:14px;
         min-height:160px;color:#7ec8e3;
         font-family:'Courier New',monospace}
.display-topo{font-size:11px;color:#3a7a9e;margin-bottom:10px;
              display:flex;justify-content:space-between}
.digitos{font-size:52px;color:#ffdf00;text-align:center;
         letter-spacing:12px;margin:8px 0 6px;font-weight:bold}
.candidato-nome{font-size:22px;color:#fff;text-align:center;
                font-weight:bold;margin-top:4px}
.candidato-partido{font-size:13px;color:#7ec8e3;text-align:center;
                   margin-top:2px}
.msg-display{font-size:15px;color:#ffdf00;text-align:center;
             margin-top:10px}
.vazio{color:#2a4a6e;font-size:18px;text-align:center;
       margin-top:30px}
.teclado{display:grid;grid-template-columns:repeat(3,1fr);
         gap:8px;margin-bottom:12px}
.teclado button{height:58px;font-size:24px;font-weight:bold;
                border:none;border-radius:8px;cursor:pointer;
                color:#fff;
                background:linear-gradient(180deg,#1b8a1b,#157015);
                border:1px solid #0f550f;
                box-shadow:0 3px 0 #0a400a;transition:all .08s}
.teclado button:active{transform:translateY(2px);
    box-shadow:0 1px 0 #0a400a;
    background:linear-gradient(180deg,#157015,#0f550f)}
.btn-branco{background:linear-gradient(180deg,#f0f0f0,#ccc)!important;
            color:#222!important;border:1px solid #aaa!important}
.btn-cinza{background:linear-gradient(180deg,#888,#666)!important;
           border:1px solid #555!important}
.acoes{display:flex;gap:8px}
.acoes button{flex:1;height:50px;font-size:15px;font-weight:bold;
              border:none;border-radius:8px;cursor:pointer;
              color:#fff;letter-spacing:1px;transition:all .08s}
.acoes button:active{transform:translateY(2px)}
.btn-conf{background:linear-gradient(180deg,#2e7d32,#1b5e20);
          box-shadow:0 3px 0 #0d3d10}
.btn-conf:active{box-shadow:0 1px 0 #0d3d10}
.tela{display:none}
.tela.ativa{display:block}
h1{color:#fff;font-size:18px;text-align:center;margin-bottom:18px;
   letter-spacing:1px}
p{color:#ccc;font-size:14px;text-align:center;margin:6px 0}
.login-input{width:100%;padding:14px;font-size:24px;
             text-align:center;
             border:2px solid #1a3a5c;border-radius:8px;
             background:#0a1628;
             color:#ffdf00;letter-spacing:4px;margin-bottom:14px;
             font-family:'Courier New',monospace}
.login-input:focus{outline:none;border-color:#ffdf00}
.login-input:disabled{opacity:.4;cursor:not-allowed}
.btn-entrar{width:100%;height:50px;font-size:18px;
            font-weight:bold;border:none;border-radius:8px;
            cursor:pointer;color:#fff;
            background:linear-gradient(180deg,#1565c0,#0d47a1);
            box-shadow:0 3px 0 #0a3060;letter-spacing:2px}
.btn-entrar:active{transform:translateY(2px);
                   box-shadow:0 1px 0 #0a3060}
.btn-entrar:disabled{opacity:.5;cursor:wait}
.btn-gerar{width:100%;height:50px;font-size:16px;
           font-weight:bold;border:none;border-radius:8px;
           cursor:pointer;color:#fff;
           background:linear-gradient(180deg,#f9a825,#f57f17);
           box-shadow:0 3px 0 #c17900;letter-spacing:1px;
           margin-top:8px}
.btn-gerar:active{transform:translateY(2px);
                  box-shadow:0 1px 0 #c17900}
.btn-gerar:disabled{opacity:.5;cursor:not-allowed}
.erro{color:#ff5252;font-size:13px;text-align:center;
      margin-top:10px;min-height:18px}
.fechar-msg{background:#2b0d0d;border:2px solid #b71c1c;
            border-radius:8px;padding:16px;margin-bottom:14px;
            text-align:center}
.fechar-msg h2{color:#ff5252;font-size:16px;margin-bottom:8px}
.fechar-msg p{color:#aaa;font-size:13px}
.contador{color:#ffdf00;font-size:36px;
          font-family:'Courier New',monospace;
          text-align:center;margin-top:12px;font-weight:bold}
.contador-label{color:#7ec8e3;font-size:12px;text-align:center;
                margin-top:4px;letter-spacing:2px}
.caixa-conf{background:#0a1628;border:2px solid #1a3a5c;
            border-radius:8px;padding:20px;margin-bottom:14px}
.btn-largo{width:100%;height:52px;font-size:16px;
           font-weight:bold;border:none;border-radius:8px;
           cursor:pointer;color:#fff;margin-top:8px;
           letter-spacing:1px}
.btn-verde{background:linear-gradient(180deg,#2e7d32,#1b5e20);
           box-shadow:0 3px 0 #0d3d10}
.btn-vermelho{background:linear-gradient(180deg,#c62828,#b71c1c);
              box-shadow:0 3px 0 #7f0000}
.btn-cinza-full{background:linear-gradient(180deg,#555,#333);
                box-shadow:0 3px 0 #222}
.btn-largo:active{transform:translateY(2px)}
.fim-msg{color:#fff;font-size:22px;text-align:center;
         margin:20px 0;font-weight:bold}
.fim-sub{color:#7ec8e3;font-size:14px;text-align:center}
.aguarde{color:#7ec8e3;font-size:14px;text-align:center;
         margin-top:10px;display:none}
.zerada-box{display:none;margin-top:14px;padding:12px;
            border-radius:6px;font-size:13px;
            text-align:center;line-height:1.5}
</style>
</head>
<body>
<div class="urna">
  <div class="topo">
    <span class="topo-titulo">JUSTIÇA ELEITORAL</span>
    <span class="topo-brasil">BRASIL</span>
  </div>

  <!-- TELA: CONTADOR (antes das 10:39) -->
  <div id="tela-contador" class="tela">
    <div class="fechar-msg">
      <h2>ELEIÇÃO AINDA NÃO INICIADA</h2>
      <p>Abre em {{ abre_full }} (Brasília)</p>
      <div class="contador" id="contador">00:00:00</div>
      <div class="contador-label">PARA A ABERTURA</div>
      <div id="status-zerada" class="zerada-box"></div>
    </div>
    <p style="color:#555;font-size:12px;text-align:center">
      Aguarde o horário de abertura para digitar seu título.</p>
  </div>

  <!-- TELA: CONTADOR (depois do encerramento) -->
  <div id="tela-encerrada" class="tela">
    <div class="fechar-msg">
      <h2>ELEIÇÃO ENCERRADA</h2>
      <p>A votação encerrou em {{ fecha_full }}
         (Brasília).</p>
      <p style="margin-top:14px">
        <a href="/apuracao"
           style="color:#ffdf00;font-weight:bold">
          VER APURAÇÃO E INTEGRIDADE &#9654;</a></p>
    </div>
  </div>

  <!-- TELA: LOGIN -->
  <div id="tela-login" class="tela ativa">
    <h1>DIGITE SEU TÍTULO DE ELEITOR</h1>
    <input type="text" id="titulo" class="login-input"
           maxlength="12" placeholder="000 000 000 000"
           autocomplete="off">
    <button class="btn-entrar" id="btn-entrar"
            onclick="entrar()">ENTRAR</button>
    <button class="btn-gerar" id="btn-gerar-topo"
            onclick="location.href='/gerar'">
      GERAR TÍTULO
    </button>
    <p id="erro-login" class="erro"></p>
  </div>

  <!-- TELA: URNA -->
  <div id="tela-urna" class="tela">
    <div class="display">
      <div class="display-topo">
        <span>VOTAÇÃO</span>
        <span id="display-num"></span>
      </div>
      <div id="display-corpo">
        <div class="vazio">Digite o número do candidato</div>
      </div>
    </div>
    <div class="teclado">
      <button onclick="tecla('1')">1</button>
      <button onclick="tecla('2')">2</button>
      <button onclick="tecla('3')">3</button>
      <button onclick="tecla('4')">4</button>
      <button onclick="tecla('5')">5</button>
      <button onclick="tecla('6')">6</button>
      <button onclick="tecla('7')">7</button>
      <button onclick="tecla('8')">8</button>
      <button onclick="tecla('9')">9</button>
      <button class="btn-branco" onclick="branco()">BRANCO
      </button>
      <button onclick="tecla('0')">0</button>
      <button class="btn-cinza" onclick="corrigirTeclado()">
        CORRIGIR</button>
    </div>
    <div class="acoes">
      <button class="btn-conf" onclick="confirmarTeclado()">
        CONFIRMAR</button>
    </div>
  </div>

  <!-- TELA: CONFIRMAÇÃO -->
  <div id="tela-confirmacao" class="tela">
    <div id="conf-cabecalho"></div>
    <div id="conf-corpo" class="caixa-conf"></div>
    <button class="btn-largo btn-cinza-full"
            onclick="voltarUrna()">CORRIGIR</button>
    <button class="btn-largo btn-verde"
            onclick="irParaRevisao()">CONFIRMAR</button>
  </div>

  <!-- TELA: REVISÃO -->
  <div id="tela-revisao" class="tela">
    <h1>CONFIRA SEU VOTO</h1>
    <div id="resumo-voto" class="caixa-conf"></div>
    <button class="btn-largo btn-cinza-full"
            onclick="voltarUrna()">CORRIGIR</button>
    <button class="btn-largo btn-verde"
            onclick="irParaFinal()">CONFIRMAR</button>
  </div>

  <!-- TELA: ÚLTIMA CONFIRMAÇÃO -->
  <div id="tela-final" class="tela">
    <h1>ÚLTIMA CONFIRMAÇÃO</h1>
    <p>Seu voto será registrado definitivamente.</p>
    <p>Depois desta confirmação não será possível alterá-lo.</p>
    <br>
    <button class="btn-largo btn-cinza-full"
            onclick="voltarRevisao()">VOLTAR</button>
    <button class="btn-largo btn-vermelho"
            onclick="registrarVoto()">CONFIRMAR VOTO</button>
    <p id="aguarde" class="aguarde">Aguarde...</p>
  </div>

  <!-- TELA: FIM -->
  <div id="tela-fim" class="tela">
    <div class="fim-msg">VOTO REGISTRADO</div>
    <div class="fim-sub">Obrigado pela participação.</div>
  </div>
</div>

<script>
var CANDIDATOS = {{ cand_json }};
var JANELA = {{ janela_json }};
var token = null;
var digitos = "";
var votoEscolhido = null;
var timerId = null;

function mostrarTela(id) {
  var t = document.querySelectorAll(".tela");
  for (var i = 0; i < t.length; i++)
    t[i].classList.remove("ativa");
  document.getElementById(id).classList.add("ativa");
}

function fmt(s) {
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  return (h < 10 ? "0" : "") + h + ":" +
         (m < 10 ? "0" : "") + m + ":" +
         (sec < 10 ? "0" : "") + sec;
}

function bloquearInput(bloqueado) {
  document.getElementById("titulo").disabled = bloqueado;
  document.getElementById("btn-entrar").disabled = bloqueado;
  document.getElementById("btn-gerar-topo").disabled =
    bloqueado;
}

function tick() {
  var agora = Math.floor(Date.now() / 1000);
  var abreTs = JANELA.abre_ts;
  var fechaTs = JANELA.fecha_ts;

  if (agora < abreTs) {
    // ANTES de abrir — contador + bloqueia input
    var restante = abreTs - agora;
    document.getElementById("contador").textContent =
      fmt(restante);
    mostrarTela("tela-contador");
    bloquearInput(true);
    return;
  }
  if (agora >= fechaTs) {
    // DEPOIS de fechar
    mostrarTela("tela-encerrada");
    bloquearInput(true);
    if (timerId) clearInterval(timerId);
    return;
  }
  // ABERTA — libera input
  bloquearInput(false);
  if (timerId) clearInterval(timerId);
}

async function carregarZerada() {
  try {
    var resp = await fetch("/api/apuracao",
                           {method: "POST"});
    if (resp.status !== 200) return;
    var d = await resp.json();
    var el = document.getElementById("status-zerada");
    if (!el) return;
    el.style.display = "block";
    if (d.total_votos === 0 && d.eleitores_votaram === 0) {
      el.style.background = "#0d2b0d";
      el.style.color = "#66bb6a";
      el.style.border = "1px solid #1b5e20";
      el.innerHTML =
        "<strong>URNA ZERADA</strong><br>0 votos · " +
        d.eleitores_cadastrados + " eleitores · " +
        d.titulos_disponiveis +
        " títulos disponíveis<br>" +
        '<a href="/apuracao" style="color:#ffdf00">' +
        "Verificar publicamente &#9654;</a>";
    } else {
      el.style.background = "#2b0d0d";
      el.style.color = "#ef5350";
      el.style.border = "1px solid #b71c1c";
      el.innerHTML =
        "<strong>URNA NÃO ESTÁ ZERADA</strong><br>" +
        d.total_votos + " voto(s) já registrado(s).";
    }
  } catch (e) {}
}

function iniciarContador() {
  tick();
  carregarZerada();
  setInterval(function () {
    if (Math.floor(Date.now() / 1000) < JANELA.abre_ts)
      carregarZerada();
  }, 60000);
  timerId = setInterval(tick, 1000);
}

/* --- Login --- */
async function entrar() {
  if (!JANELA || !janelaAberta()) return;
  var titulo =
    document.getElementById("titulo").value.trim();
  var erroEl = document.getElementById("erro-login");
  var btn = document.getElementById("btn-entrar");
  erroEl.textContent = "";
  if (!titulo) {
    erroEl.textContent = "Digite seu título.";
    return;
  }
  btn.disabled = true;
  try {
    var resp = await fetch("/api/autorizar", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({titulo: titulo})
    });
    var dados = await resp.json();
    if (!resp.ok) {
      erroEl.innerHTML =
        "<strong>" + dados.erro + "</strong><br>" +
        (dados.detalhe || "");
      btn.disabled = false;
      return;
    }
    token = dados.token;
    mostrarTela("tela-urna");
    atualizarDisplay();
  } catch (e) {
    erroEl.textContent = "Erro de conexão.";
    btn.disabled = false;
  }
}

function janelaAberta() {
  var agora = Math.floor(Date.now() / 1000);
  return agora >= JANELA.abre_ts &&
         agora < JANELA.fecha_ts;
}

/* --- Teclado --- */
function atualizarDisplay() {
  var d = document.getElementById("display-num");
  var c = document.getElementById("display-corpo");
  if (digitos === "") {
    d.textContent = "";
    c.innerHTML =
      '<div class="vazio">Digite o número do candidato</div>';
  } else {
    d.textContent = digitos;
    if (CANDIDATOS[digitos]) {
      var i = CANDIDATOS[digitos];
      c.innerHTML =
        '<div class="candidato-nome">' + i.nome + '</div>' +
        '<div class="candidato-partido">' + i.partido +
        '</div>' +
        '<div class="candidato-partido">Número: ' +
        digitos + '</div>';
    } else {
      c.innerHTML =
        '<div class="msg-display">NÚMERO INVÁLIDO</div>';
    }
  }
}

function tecla(n) {
  if (digitos.length >= 2) return;
  digitos += n;
  atualizarDisplay();
}

function corrigirTeclado() {
  digitos = ""; atualizarDisplay();
}

function branco() {
  votoEscolhido = "branco";
  document.getElementById("conf-cabecalho").innerHTML =
    "<h1>VOTO EM BRANCO</h1>";
  document.getElementById("conf-corpo").innerHTML =
    "<p>Você escolheu <strong style='color:#ffdf00'>" +
    "VOTO BRANCO</strong>.</p>";
  mostrarTela("tela-confirmacao");
}

function confirmarTeclado() {
  if (digitos === "") return;
  if (CANDIDATOS[digitos]) {
    votoEscolhido = digitos;
    mostrarRevisao();
  } else {
    votoEscolhido = "nulo";
    document.getElementById("conf-cabecalho").innerHTML =
      "<h1 style='color:#ff5252'>ATENÇÃO</h1>";
    document.getElementById("conf-corpo").innerHTML =
      "<p>Este número não corresponde a nenhum candidato."
      + "</p>" +
      "<p>Seu voto será considerado " +
      "<strong style='color:#ff5252'>NULO</strong>.</p>" +
      "<p>Deseja continuar?</p>";
    mostrarTela("tela-confirmacao");
  }
}

function mostrarRevisao() {
  var html;
  if (votoEscolhido === "branco") {
    html = "<p style='font-size:20px;color:#ffdf00'>" +
           "<strong>VOTO BRANCO</strong></p>";
  } else if (votoEscolhido === "nulo") {
    html = "<p style='font-size:20px;color:#ff5252'>" +
           "<strong>VOTO NULO</strong></p>";
  } else {
    var i = CANDIDATOS[votoEscolhido];
    html =
      "<p style='font-size:20px;color:#fff'>" +
      "<strong>" + i.nome + "</strong></p>" +
      "<p style='color:#7ec8e3'>" + i.partido + "</p>" +
      "<p style='color:#ffdf00'>Número: <strong>" +
      votoEscolhido + "</strong></p>";
  }
  document.getElementById("resumo-voto").innerHTML = html;
  mostrarTela("tela-revisao");
}

function irParaRevisao() { mostrarRevisao(); }
function irParaFinal()   { mostrarTela("tela-final"); }
function voltarRevisao() { mostrarRevisao(); }

function voltarUrna() {
  digitos = "";
  votoEscolhido = null;
  atualizarDisplay();
  mostrarTela("tela-urna");
}

async function registrarVoto() {
  if (!janelaAberta()) {
    var agora = Math.floor(Date.now() / 1000);
    alert(agora < JANELA.abre_ts
      ? "Eleição ainda não iniciada."
      : "Eleição encerrada.");
    return;
  }
  var ag = document.getElementById("aguarde");
  ag.style.display = "block";
  try {
    var resp = await fetch("/api/voto", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(
        {token: token, voto: votoEscolhido})
    });
    ag.style.display = "none";
    if (resp.ok) {
      mostrarTela("tela-fim");
    } else {
      var dados = await resp.json();
      alert(dados.erro || "Erro ao registrar voto.");
    }
  } catch (e) {
    ag.style.display = "none";
    alert("Erro de conexão.");
  }
}

document.getElementById("titulo")
  .addEventListener("keydown", function (e) {
    if (e.key === "Enter") entrar();
  });

iniciarContador();

(function manterAtiva() {
  function ping() {
    fetch("/health").then(function () {
      setTimeout(ping, 420000);
    }).catch(function () {
      setTimeout(ping, 30000);
    });
  }
  ping();
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# HTML — Gerar título
# ---------------------------------------------------------------------------
HTML_GERAR = r"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gerar Título</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#006400;font-family:Arial,sans-serif;
     display:flex;justify-content:center;min-height:100vh;
     padding:20px}
.painel{background:#004d00;border:3px solid #003300;
        border-radius:16px;padding:24px;width:440px;
        box-shadow:0 8px 32px rgba(0,0,0,.5)}
.topo{background:#006b00;border-radius:8px;padding:10px 14px;
      display:flex;justify-content:space-between;
      align-items:center;margin-bottom:14px;
      border:1px solid #008c00}
.topo-titulo{color:#fff;font-size:13px;font-weight:bold;
             letter-spacing:2px}
.topo-brasil{color:#ffdf00;font-size:11px;font-weight:bold}
h1{color:#fff;font-size:18px;text-align:center;
   margin-bottom:16px;letter-spacing:1px}
p{color:#ccc;font-size:14px;text-align:center;margin:6px 0}
.fechar-msg{background:#2b0d0d;border:2px solid #b71c1c;
            border-radius:8px;padding:16px;
            margin-bottom:14px;text-align:center}
.fechar-msg h2{color:#ff5252;font-size:16px;margin-bottom:8px}
.fechar-msg p{color:#aaa;font-size:13px}
.contador{color:#ffdf00;font-size:36px;
          font-family:'Courier New',monospace;
          text-align:center;margin-top:12px;font-weight:bold}
.contador-label{color:#7ec8e3;font-size:12px;
                text-align:center;margin-top:4px;
                letter-spacing:2px}
.tela{display:none}
.tela.ativa{display:block}
.btn{display:block;width:100%;height:52px;font-size:17px;
     font-weight:bold;border:none;border-radius:8px;
     cursor:pointer;color:#fff;letter-spacing:1px;
     margin-top:10px}
.btn:active{transform:translateY(2px)}
.btn-gerar{background:linear-gradient(180deg,#f9a825,#f57f17);
           box-shadow:0 3px 0 #c17900}
.btn-gerar:disabled{opacity:.5;cursor:not-allowed}
.btn-copiar{background:linear-gradient(180deg,#1565c0,#0d47a1);
            box-shadow:0 3px 0 #0a3060}
.btn-voltar{background:linear-gradient(180deg,#555,#333);
            box-shadow:0 3px 0 #222}
.titulo-box{background:#0a1628;border:2px solid #ffdf00;
            border-radius:8px;padding:20px;text-align:center;
            margin:14px 0}
.titulo-valor{color:#ffdf00;font-size:32px;
              font-family:'Courier New',monospace;
              letter-spacing:4px;font-weight:bold}
.aviso{color:#aaa;font-size:12px;text-align:center;
       margin-top:8px}
.erro{color:#ff5252;font-size:13px;text-align:center;
      margin-top:10px;min-height:18px}
.ok{color:#66bb6a;font-size:14px;text-align:center;
    margin-top:8px}
.info-caixa{background:#0a1628;border:2px solid #1a3a5c;
            border-radius:8px;padding:16px;margin-bottom:14px}
.info-caixa p{color:#7ec8e3;font-size:13px}
</style>
</head>
<body>
<div class="painel">
  <div class="topo">
    <span class="topo-titulo">JUSTIÇA ELEITORAL</span>
    <span class="topo-brasil">BRASIL</span>
  </div>

  <!-- CONTADOR -->
  <div id="tela-contador" class="tela">
    <div class="fechar-msg">
      <h2>ELEIÇÃO AINDA NÃO INICIADA</h2>
      <p>Abre em {{ abre_full }} (Brasília)</p>
      <div class="contador" id="contador">00:00:00</div>
      <div class="contador-label">PARA A ABERTURA</div>
      <p style="margin-top:12px">
        <a href="/apuracao" style="color:#ffdf00">
          Conferir urna zerada &#9654;</a></p>
    </div>
    <button class="btn btn-voltar"
            onclick="location.href='/'">VOLTAR</button>
  </div>

  <!-- ENCERRADA -->
  <div id="tela-encerrada" class="tela">
    <div class="fechar-msg">
      <h2>ELEIÇÃO ENCERRADA</h2>
      <p>A votação encerrou em {{ fecha_full }}.</p>
      <p style="margin-top:12px">
        <a href="/apuracao" style="color:#ffdf00">
          Ver apuração e integridade &#9654;</a></p>
    </div>
    <button class="btn btn-voltar"
            onclick="location.href='/'">VOLTAR</button>
  </div>

  <!-- JÁ TEM TÍTULO -->
  <div id="tela-ja-tem" class="tela">
    <h1>VOCÊ JÁ TEM UM TÍTULO</h1>
    <div class="titulo-box">
      <div class="titulo-valor" id="titulo-existente">
      </div>
    </div>
    <p class="aviso">Guarde este título. Ele já foi
      gerado e não pode ser gerado novamente.</p>
    <button class="btn btn-copiar"
            onclick="copiarExistente()">COPIAR TÍTULO</button>
    <p class="ok" id="ok-copiar" style="display:none">
      Copiado!</p>
    <button class="btn btn-voltar"
            onclick="location.href='/'">IR PARA VOTAÇÃO
    </button>
  </div>

  <!-- GERAR -->
  <div id="tela-gerar" class="tela">
    <h1>GERAR TÍTULO DE ELEITOR</h1>
    <div class="info-caixa">
      <p>Clique para gerar um título de eleitor único.</p>
      <p>O título será removido da lista e não poderá
         ser gerado novamente.</p>
      <p>Ele ficará salvo neste navegador.</p>
    </div>
    <button class="btn btn-gerar" id="btn-gerar"
            onclick="gerar()">GERAR TÍTULO</button>
    <p class="erro" id="erro-gerar"></p>
    <button class="btn btn-voltar"
            onclick="location.href='/'">VOLTAR</button>
  </div>

  <!-- TÍTULO GERADO -->
  <div id="tela-gerado" class="tela">
    <h1>SEU TÍTULO</h1>
    <div class="titulo-box">
      <div class="titulo-valor" id="titulo-valor"></div>
    </div>
    <p class="aviso">Copie e guarde este título.
      Ele não pode ser gerado novamente.</p>
    <button class="btn btn-copiar"
            onclick="copiarTitulo()">COPIAR TÍTULO</button>
    <p class="ok" id="ok-copiar2" style="display:none">
      Copiado!</p>
    <button class="btn btn-voltar"
            onclick="location.href='/'">IR PARA VOTAÇÃO
    </button>
  </div>

  <!-- SEM TÍTULOS -->
  <div id="tela-sem" class="tela">
    <h1>SEM TÍTULOS DISPONÍVEIS</h1>
    <p>Todos os títulos já foram gerados.</p>
    <button class="btn btn-voltar"
            onclick="location.href='/'">VOLTAR</button>
  </div>
</div>

<script>
var JANELA = {{ janela_json }};
var timerId = null;

function mostrarTela(id) {
  var t = document.querySelectorAll(".tela");
  for (var i = 0; i < t.length; i++)
    t[i].classList.remove("ativa");
  document.getElementById(id).classList.add("ativa");
}

function fmt(s) {
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  return (h < 10 ? "0" : "") + h + ":" +
         (m < 10 ? "0" : "") + m + ":" +
         (sec < 10 ? "0" : "") + sec;
}

function bloquearTudo(b) {
  var btn = document.getElementById("btn-gerar");
  if (btn) btn.disabled = b;
}

function tick() {
  var agora = Math.floor(Date.now() / 1000);
  if (agora < JANELA.abre_ts) {
    document.getElementById("contador").textContent =
      fmt(JANELA.abre_ts - agora);
    mostrarTela("tela-contador");
    bloquearTudo(true);
    return;
  }
  if (agora >= JANELA.fecha_ts) {
    mostrarTela("tela-encerrada");
    bloquearTudo(true);
    if (timerId) clearInterval(timerId);
    return;
  }
  bloquearTudo(false);
  initAberta();
  if (timerId) { clearInterval(timerId); timerId = null; }
}

function janelaAberta() {
  var agora = Math.floor(Date.now() / 1000);
  return agora >= JANELA.abre_ts &&
         agora < JANELA.fecha_ts;
}

function meuTitulo() {
  return localStorage.getItem("titulo_eleitor");
}
function salvarTitulo(t) {
  localStorage.setItem("titulo_eleitor", t);
}

function copiarTexto(txt, idOk) {
  navigator.clipboard.writeText(txt).then(function () {
    var el = document.getElementById(idOk);
    el.style.display = "block";
    setTimeout(function () {
      el.style.display = "none";
    }, 2000);
  });
}
function copiarTitulo() {
  copiarTexto(meuTitulo() || "", "ok-copiar2");
}
function copiarExistente() {
  copiarTexto(meuTitulo() || "", "ok-copiar");
}

async function gerar() {
  if (!janelaAberta()) return;
  var btn = document.getElementById("btn-gerar");
  var erroEl = document.getElementById("erro-gerar");
  erroEl.textContent = "";
  btn.disabled = true;
  try {
    var resp = await fetch("/api/gerar", {method: "POST"});
    var dados = await resp.json();
    if (!resp.ok) {
      erroEl.textContent =
        dados.erro || "Erro ao gerar.";
      btn.disabled = false;
      return;
    }
    salvarTitulo(dados.titulo);
    document.getElementById("titulo-valor").textContent =
      dados.titulo;
    mostrarTela("tela-gerado");
  } catch (e) {
    erroEl.textContent = "Erro de conexão.";
    btn.disabled = false;
  }
}

var jaIniciado = false;
function initAberta() {
  if (jaIniciado) return;
  jaIniciado = true;
  var t = meuTitulo();
  if (t) {
    document.getElementById("titulo-existente")
      .textContent = t;
    mostrarTela("tela-ja-tem");
  } else {
    mostrarTela("tela-gerar");
  }
}

(function init() {
  tick();
  if (janelaAberta()) return;
  timerId = setInterval(tick, 1000);
})();

(function manterAtiva() {
  function ping() {
    fetch("/health").then(function () {
      setTimeout(ping, 420000);
    }).catch(function () {
      setTimeout(ping, 30000);
    });
  }
  ping();
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# HTML — Apuração
# ---------------------------------------------------------------------------
HTML_APURACAO = r"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Apuração</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#006400;font-family:Arial,sans-serif;
     display:flex;justify-content:center;min-height:100vh;
     padding:20px}
.painel{background:#004d00;border:3px solid #003300;
        border-radius:16px;padding:24px;width:460px;
        box-shadow:0 8px 32px rgba(0,0,0,.5)}
h1{color:#ffdf00;font-size:20px;text-align:center;
   margin-bottom:18px;letter-spacing:2px}
.btn{display:block;width:100%;height:48px;font-size:15px;
     font-weight:bold;border:none;border-radius:8px;
     cursor:pointer;color:#fff;margin-bottom:10px;
     letter-spacing:1px}
.btn-azul{background:linear-gradient(180deg,#1565c0,#0d47a1);
          box-shadow:0 3px 0 #0a3060}
.btn-cinza{background:linear-gradient(180deg,#555,#333);
           box-shadow:0 3px 0 #222}
.btn:active{transform:translateY(2px)}
.secao{background:#0a1628;border:2px solid #1a3a5c;
       border-radius:8px;padding:14px;margin-top:14px}
.secao-titulo{color:#ffdf00;font-size:13px;font-weight:bold;
              letter-spacing:2px;margin-bottom:10px;
              border-bottom:1px solid #1a3a5c;
              padding-bottom:6px}
.linha{display:flex;justify-content:space-between;
       padding:6px 0;color:#ccc;font-size:15px;
       border-bottom:1px solid #111}
.linha span:last-child{color:#7ec8e3;font-weight:bold}
.st{margin-top:14px;padding:12px;border-radius:6px;
    font-size:14px;display:none}
.st-ok{background:#0d2b0d;color:#66bb6a;
       border:1px solid #1b5e20}
.st-erro{background:#2b0d0d;color:#ef5350;
         border:1px solid #b71c1c}
.st-aviso{background:#2b230d;color:#ffca28;
          border:1px solid #f57f17}
.btn:disabled{opacity:.4;cursor:not-allowed}
</style>
</head>
<body>
<div class="painel">
  <h1>APURAÇÃO</h1>
  <div id="fase-msg" class="st"></div>
  <button class="btn btn-azul" id="btn-apurar"
          onclick="apurar()">
    [ APURAR ]</button>
  <button class="btn btn-cinza" id="btn-verificar"
          onclick="verificarIntegridade()">
    [ VERIFICAR INTEGRIDADE ]</button>
  <div id="zerada" class="st"></div>
  <div id="resultado" style="display:none">
    <div class="secao">
      <div class="secao-titulo">ELEITORES</div>
      <div class="linha"><span>Cadastrados</span>
        <span id="cadastrados">–</span></div>
      <div class="linha"><span>Votaram</span>
        <span id="votaram">–</span></div>
      <div class="linha"><span>Abstenções</span>
        <span id="abstencoes">–</span></div>
      <div class="linha"><span>Títulos no pool</span>
        <span id="pool">–</span></div>
    </div>
    <div class="secao">
      <div class="secao-titulo">VOTOS</div>
      <div class="linha"><span id="nome-c1">–</span>
        <span id="c1">–</span></div>
      <div class="linha"><span id="nome-c2">–</span>
        <span id="c2">–</span></div>
      <div class="linha"><span id="nome-c3">–</span>
        <span id="c3">–</span></div>
      <div class="linha"><span id="nome-c4">–</span>
        <span id="c4">–</span></div>
      <div class="linha"><span id="nome-c5">–</span>
        <span id="c5">–</span></div>
      <div class="linha"><span>Brancos</span>
        <span id="brancos">–</span></div>
      <div class="linha"><span>Nulos</span>
        <span id="nulos">–</span></div>
      <div class="linha"><span>Votos válidos</span>
        <span id="validos">–</span></div>
      <div class="linha"><span>Total de votos</span>
        <span id="total">–</span></div>
    </div>
    <div class="secao">
      <div class="secao-titulo">SEGUNDO TURNO</div>
      <div id="segundo-turno"
           style="color:#ccc;font-size:14px">–</div>
    </div>
  </div>
  <div id="integridade" class="st"></div>
</div>

<script>
var CAND = {{ cand_json }};
var JANELA = {{ janela_json }};
var faseVista = null;

function faseAtual() {
  var agora = Math.floor(Date.now() / 1000);
  if (agora < JANELA.abre_ts) return "antes";
  if (agora < JANELA.fecha_ts) return "durante";
  return "depois";
}

function mostrarFase() {
  var f = faseAtual();
  var el = document.getElementById("fase-msg");
  var bAp = document.getElementById("btn-apurar");
  var bVe = document.getElementById("btn-verificar");
  if (f === "durante") {
    bAp.disabled = true;
    bVe.disabled = true;
    el.className = "st st-erro";
    el.style.display = "block";
    el.innerHTML =
      "<strong>APURAÇÃO E VERIFICAÇÃO BLOQUEADAS" +
      "</strong><br>Votação em andamento. Liberadas após " +
      JANELA.fecha_full + " (Brasília).";
    document.getElementById("resultado").style.display =
      "none";
    document.getElementById("integridade").style.display =
      "none";
    document.getElementById("zerada").style.display = "none";
    return;
  }
  bAp.disabled = false;
  bVe.disabled = false;
  el.style.display = "block";
  if (f === "antes") {
    el.className = "st st-aviso";
    el.innerHTML =
      "<strong>CONFERÊNCIA PRÉ-VOTAÇÃO</strong><br>" +
      "A urna deve estar zerada antes da abertura em " +
      JANELA.abre_full + ".";
  } else {
    el.className = "st st-ok";
    el.innerHTML =
      "<strong>VOTAÇÃO ENCERRADA</strong><br>" +
      "Apuração e verificação liberadas para todos.";
  }
  if (faseVista !== f) {
    faseVista = f;
    apurar();
    verificarIntegridade();
  }
}

async function apurar() {
  if (faseAtual() === "durante") { mostrarFase(); return; }
  try {
    var resp = await fetch("/api/apuracao",
                           {method: "POST"});
    if (resp.status === 403) {
      var e403 = await resp.json();
      alert(e403.erro + " " + (e403.detalhe || ""));
      return;
    }
    if (!resp.ok) {
      alert("Erro: " + resp.status); return;
    }
    var d = await resp.json();
    document.getElementById("cadastrados").textContent =
      d.eleitores_cadastrados;
    document.getElementById("votaram").textContent =
      d.eleitores_votaram;
    document.getElementById("abstencoes").textContent =
      d.abstencoes;
    document.getElementById("pool").textContent =
      d.titulos_disponiveis;
    document.getElementById("brancos").textContent =
      d.brancos;
    document.getElementById("nulos").textContent =
      d.nulos;
    document.getElementById("validos").textContent =
      d.votos_validos;
    document.getElementById("total").textContent =
      d.total_votos;
    var nums = Object.keys(CAND);
    for (var i = 0; i < nums.length; i++) {
      var n = nums[i], idx = i + 1;
      document.getElementById("nome-c" + idx)
        .textContent = CAND[n].nome + " (" + n + ")";
      document.getElementById("c" + idx).textContent =
        d["cand_" + n] || 0;
    }
    var st = d.segundo_turno;
    if (st.ha_segundo_turno) {
      var nomes = st.candidatos.map(
        function (n) { return CAND[n].nome; });
      document.getElementById("segundo-turno").innerHTML =
        "<strong style='color:#ffdf00'>SEGUNDO TURNO" +
        "</strong><br>" + nomes.join("<br>");
    } else {
      document.getElementById("segundo-turno").innerHTML =
        "<strong>NÃO HÁ SEGUNDO TURNO</strong><br>" +
        (st.motivo || "");
    }
    document.getElementById("resultado").style.display =
      "block";
    var z = document.getElementById("zerada");
    if (faseAtual() === "antes") {
      if (d.total_votos === 0 && d.eleitores_votaram === 0) {
        z.className = "st st-ok";
        z.innerHTML =
          "<strong>URNA ZERADA</strong><br>0 votos · " +
          d.eleitores_cadastrados + " eleitores · " +
          d.titulos_disponiveis +
          " títulos disponíveis";
      } else {
        z.className = "st st-erro";
        z.innerHTML =
          "<strong>URNA NÃO ESTÁ ZERADA</strong><br>" +
          d.total_votos + " voto(s) já registrado(s).";
      }
      z.style.display = "block";
    } else {
      z.style.display = "none";
    }
  } catch (e) { alert("Erro de conexão."); }
}

async function verificarIntegridade() {
  if (faseAtual() === "durante") { mostrarFase(); return; }
  try {
    var resp = await fetch("/api/verificar-integridade",
                           {method: "POST"});
    if (resp.status === 403) {
      var e403 = await resp.json();
      alert(e403.erro + " " + (e403.detalhe || ""));
      return;
    }
    var d = await resp.json();
    var el = document.getElementById("integridade");
    if (d.integridade === "OK") {
      el.className = "st st-ok";
      el.innerHTML =
        "<strong>INTEGRIDADE OK</strong><br>" +
        d.detalhe;
    } else {
      el.className = "st st-erro";
      el.innerHTML =
        "<strong>ERRO DE INTEGRIDADE</strong><br>" +
        d.detalhe;
    }
    el.style.display = "block";
  } catch (e) { alert("Erro de conexão."); }
}

mostrarFase();
setInterval(mostrarFase, 1000);

(function manterAtiva() {
  function ping() {
    fetch("/health").then(function () {
      setTimeout(ping, 420000);
    }).catch(function () {
      setTimeout(ping, 30000);
    });
  }
  ping();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Render helper
# ---------------------------------------------------------------------------
def _render(html, **ctx):
    for k, v in ctx.items():
        html = html.replace("{{ " + k + " }}", str(v))
    return html, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }


def _ctx():
    return {
        "cand_json": json.dumps(CANDIDATOS,
                                ensure_ascii=False),
        "janela_json": json.dumps(info_janela()),
        "abre_full": str_janela(ABRE_DT),
        "fecha_full": str_janela(FECHA_DT),
    }


# ---------------------------------------------------------------------------
# Rotas de página
# ---------------------------------------------------------------------------
@app.route("/")
def pagina_eleitor():
    return _render(HTML_PRINCIPAL, **_ctx())


@app.route("/gerar")
def pagina_gerar():
    return _render(HTML_GERAR, **_ctx())


@app.route("/apuracao")
def pagina_apuracao():
    return _render(HTML_APURACAO, **_ctx())


@app.route("/health")
def health():
    return jsonify({"status": "ok",
                    "fase": fase(),
                    "aberta": eleicao_aberta()})


# ---------------------------------------------------------------------------
# API — gerar título
# ---------------------------------------------------------------------------
@app.route("/api/gerar", methods=["POST"])
def api_gerar():
    if not eleicao_aberta():
        return _erro_janela()

    db = get_db()
    cur = db.execute(
        "UPDATE pool_titulos SET atribuido = 1 "
        "WHERE titulo_hash = ("
        "  SELECT titulo_hash FROM pool_titulos "
        "  WHERE atribuido = 0 LIMIT 1"
        ") AND atribuido = 0 "
        "RETURNING titulo"
    )
    row = cur.fetchone()
    db.commit()

    if row is None:
        return jsonify({
            "erro": "Todos os títulos já foram gerados."
        }), 403

    return jsonify({"titulo": row["titulo"]})


# ---------------------------------------------------------------------------
# API — autorizar
# ---------------------------------------------------------------------------
@app.route("/api/autorizar", methods=["POST"])
def api_autorizar():
    if not eleicao_aberta():
        return _erro_janela()

    _limpar_expiradas()
    dados = request.get_json(silent=True) or {}
    titulo = str(dados.get("titulo") or "").strip()
    if not titulo:
        return jsonify(
            {"erro": "Informe o título."}), 400

    salt = _garantir_salt()
    titulo_hash = hash_titulo(titulo, salt)

    db = get_db()
    eleitor = db.execute(
        "SELECT votou FROM eleitores "
        "WHERE titulo_hash = ?",
        (titulo_hash,)).fetchone()

    if eleitor is None:
        return jsonify({
            "erro": "ELEITOR NÃO AUTORIZADO",
            "detalhe": "Título não encontrado."
        }), 403
    if eleitor["votou"] == 1:
        return jsonify({
            "erro": "ELEITOR NÃO AUTORIZADO",
            "detalhe": "Este eleitor já realizou "
                       "seu voto."
        }), 403

    token = secrets.token_hex(32)
    sessoes_temporarias[token] = {
        "ts": time.time(),
        "titulo_hash": titulo_hash,
    }
    return jsonify({"token": token})


# ---------------------------------------------------------------------------
# API — voto
# ---------------------------------------------------------------------------
@app.route("/api/voto", methods=["POST"])
def api_voto():
    if not eleicao_aberta():
        return _erro_janela()

    _limpar_expiradas()
    dados = request.get_json(silent=True) or {}
    token = dados.get("token")
    voto = dados.get("voto")

    sess = sessoes_temporarias.get(token)
    if sess is None:
        return jsonify(
            {"erro": "Sessão inválida ou expirada."}), 403

    if not _jitter_decorrido(token):
        return jsonify({
            "erro": "Aguarde um instante e tente "
                    "novamente."
        }), 429

    if sessoes_temporarias.pop(token, None) is None:
        return jsonify(
            {"erro": "Sessão inválida ou expirada."}), 403

    if voto not in list(CANDIDATOS.keys()) + [
            "branco", "nulo"]:
        return jsonify(
            {"erro": "Voto inválido."}), 400

    db = get_db()
    chave = carregar_chave()
    with _lock_voto:
        cur = db.execute(
            "UPDATE eleitores SET votou = 1 "
            "WHERE titulo_hash = ? AND votou = 0",
            (sess["titulo_hash"],))
        if cur.rowcount == 0:
            db.rollback()
            return jsonify({
                "erro": "ELEITOR NÃO AUTORIZADO",
                "detalhe": "Este eleitor já realizou "
                           "seu voto."
            }), 403

        ultimo = db.execute(
            "SELECT hash FROM votos "
            "ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = (ultimo["hash"]
                     if ultimo else "0" * 64)
        hash_atual = calcular_hash(chave, voto, prev_hash)
        db.execute(
            "INSERT INTO votos "
            "(voto, hash, prev_hash) VALUES (?, ?, ?)",
            (voto, hash_atual, prev_hash))
        db.commit()

    return jsonify(
        {"status": "ok", "mensagem": "Voto registrado."})


# ---------------------------------------------------------------------------
# API — apuração
# ---------------------------------------------------------------------------
@app.route("/api/apuracao", methods=["POST"])
def api_apuracao():
    bloqueio = _bloqueio_apuracao()
    if bloqueio is not None:
        return bloqueio
    db = get_db()
    total_eleitores = db.execute(
        "SELECT COUNT(*) AS n FROM eleitores"
    ).fetchone()["n"]
    total_votaram = db.execute(
        "SELECT COUNT(*) AS n FROM eleitores "
        "WHERE votou = 1").fetchone()["n"]
    titulos_pool = db.execute(
        "SELECT COUNT(*) AS n FROM pool_titulos "
        "WHERE atribuido = 0").fetchone()["n"]

    contagem = {num: 0 for num in CANDIDATOS}
    contagem["branco"] = 0
    contagem["nulo"] = 0
    for linha in db.execute(
            "SELECT voto, COUNT(*) AS n FROM votos "
            "GROUP BY voto"):
        contagem[linha["voto"]] = linha["n"]

    validos = sum(contagem[num] for num in CANDIDATOS)
    resultado = {
        "eleitores_cadastrados": total_eleitores,
        "eleitores_votaram": total_votaram,
        "abstencoes": (total_eleitores - total_votaram),
        "titulos_disponiveis": titulos_pool,
        "brancos": contagem["branco"],
        "nulos": contagem["nulo"],
        "votos_validos": validos,
        "total_votos": (validos + contagem["branco"]
                        + contagem["nulo"]),
        "segundo_turno":
            verificar_segundo_turno(contagem),
    }
    for num in CANDIDATOS:
        resultado["cand_" + num] = contagem[num]
    return jsonify(resultado)


@app.route("/api/verificar-integridade",
           methods=["POST"])
def api_verificar_integridade():
    bloqueio = _bloqueio_apuracao()
    if bloqueio is not None:
        return bloqueio
    return jsonify(verificar_integridade(get_db()))


@app.route("/api/criar-eleitor", methods=["POST"])
def api_criar_eleitor():
    if fase() != "antes":
        return jsonify({
            "erro": "Cadastro de eleitores apenas antes "
                    "da abertura da votação."
        }), 403
    dados = request.get_json(silent=True) or {}
    titulo = str(dados.get("titulo") or "").strip()
    if not titulo:
        return jsonify(
            {"erro": "Informe o título."}), 400
    salt = _garantir_salt()
    titulo_hash = hash_titulo(titulo, salt)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO eleitores "
            "(titulo_hash, votou) VALUES (?, 0)",
            (titulo_hash,))
        db.execute(
            "INSERT OR IGNORE INTO pool_titulos "
            "(titulo_hash, titulo, atribuido) "
            "VALUES (?, ?, 0)",
            (titulo_hash, titulo))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(
            {"erro": "Título já cadastrado."}), 409
    return jsonify({
        "status": "ok",
        "mensagem": f"Eleitor {titulo} criado."})


# ---------------------------------------------------------------------------
# Regra de segundo turno
# ---------------------------------------------------------------------------
def verificar_segundo_turno(contagem: dict) -> dict:
    numeros = list(CANDIDATOS.keys())
    validos = sum(contagem.get(n, 0) for n in numeros)
    if validos == 0:
        return {"ha_segundo_turno": False,
                "motivo": "Sem votos válidos."}
    ranking = sorted(numeros,
                     key=lambda n: contagem.get(n, 0),
                     reverse=True)
    if contagem.get(ranking[0], 0) > validos * 0.5:
        return {"ha_segundo_turno": False,
                "eleito": ranking[0],
                "motivo": "Maioria simples (>50%) "
                          "no primeiro turno."}
    return {"ha_segundo_turno": True,
            "candidatos": [ranking[0], ranking[1]],
            "motivo": "Nenhum candidato obteve mais de "
                      "50% dos votos válidos."}


# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    info = info_janela()
    print("=" * 50)
    print("  URNA ELETRÔNICA SIMULADA")
    print("=" * 50)
    print(f"  Eleitores:   {len(TITULOS_SEMEADOS)}")
    print(f"  Candidatos:  {len(CANDIDATOS)}")
    print(f"  Abre:        {info['abre_full']} "
          f"(Brasília)")
    print(f"  Encerra:     {info['fecha_full']} "
          f"(Brasília)")
    if info["aberta"]:
        print("  Status:      ABERTA")
    elif info["falta_segundos"] > 0:
        mins = info["falta_segundos"] // 60
        print(f"  Status:      FECHADA "
              f"(abre em {mins} min)")
    else:
        print("  Status:      ENCERRADA")
    print("  Acesso:      público "
          "(sem credencial)")
    print(f"  URL:         http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000)
