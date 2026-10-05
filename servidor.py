from flask import Flask, jsonify, send_file, request
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed
import os, re, json, base64, unicodedata, threading, time, uuid, traceback
import pandas as pd
import requests

PASTA       = os.path.dirname(os.path.abspath(__file__))
PASTA_DADOS = os.path.join(PASTA, "dados")
os.makedirs(PASTA_DADOS, exist_ok=True)

app = Flask(__name__, static_folder=PASTA)
CORS(app)

# --- Config TSE ---
UF            = "pe"
COD_PLEITO    = "6257"
COD_ELEICAO   = "6259"
COD_ELEICAO_6 = "006259"

CARGOS = {
    "ESTADUAL": {"codigo": "0007", "nome": "Deputado Estadual"},
    "FEDERAL":  {"codigo": "0006", "nome": "Deputado Federal"},
}

URL_CADASTRO = (f"https://resultados.tse.jus.br/oficial/ele2026/{COD_PLEITO}/"
                f"config/mun-e006257-cm.json")

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://resultados.tse.jus.br/"}

# --- Config GitHub ---
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN")
GITHUB_OWNER  = os.environ.get("GITHUB_OWNER")
GITHUB_REPO   = os.environ.get("GITHUB_REPO")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_OK = bool(GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO)

# --- Fila de jobs em memória ---
# jobs[job_id] = {
#   status: "baixando" | "commitando" | "ok" | "erro",
#   progresso: int, total: int,
#   nome, cargo, candidato_id, votos_total, mensagem
# }
JOBS = {}
JOBS_LOCK = threading.Lock()

# --- Helpers ---
def b64url_decode(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode())

def decode_jws(texto):
    partes = texto.strip().split(".")
    if len(partes) != 3:
        raise ValueError("Não é JWS")
    return json.loads(b64url_decode(partes[1]))

def carregar_json(texto):
    texto = texto.lstrip()
    if texto.startswith("{"):
        return json.loads(texto)
    return decode_jws(texto)

def baixar_json(url, timeout=30):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return carregar_json(r.text)

def slugify(nome):
    nome = unicodedata.normalize("NFKD", nome).encode("ascii","ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "_", nome).strip("_").upper()

def id_arquivo(cargo_key, nome):
    return f"{cargo_key}_{slugify(nome)}"

def parse_id(id_str):
    for key in CARGOS:
        if id_str.startswith(f"{key}_"):
            return key, id_str
    return "ESTADUAL", f"ESTADUAL_{id_str}"

def url_estado(cargo_key):
    c = CARGOS[cargo_key]["codigo"]
    return (f"https://resultados.tse.jus.br/oficial/ele2026/{COD_ELEICAO}/dados/"
            f"{UF}/{UF}-c{c}-e{COD_ELEICAO_6}-u.json")

def url_votos(cargo_key, cod_mun):
    c = CARGOS[cargo_key]["codigo"]
    return (f"https://resultados.tse.jus.br/oficial/ele2026/{COD_ELEICAO}/dados/"
            f"{UF}/{UF}{cod_mun}-c{c}-e{COD_ELEICAO_6}-u.json")

# --- Caches ---
_cache = {"cadastro": None, "estado": {k: None for k in CARGOS}}
_cache_lock = threading.Lock()

def get_cadastro():
    with _cache_lock:
        if _cache["cadastro"] is None:
            _cache["cadastro"] = baixar_json(URL_CADASTRO, timeout=60)
        return _cache["cadastro"]

def get_estado(cargo_key):
    with _cache_lock:
        if _cache["estado"][cargo_key] is None:
            _cache["estado"][cargo_key] = baixar_json(url_estado(cargo_key), timeout=60)
        return _cache["estado"][cargo_key]

def municipios_pe():
    for abr in get_cadastro().get("abr", []):
        if abr.get("cd") == UF:
            return abr.get("mu", [])
    return []

def candidatos_pe(cargo_key):
    lista = []
    for cargo in get_estado(cargo_key).get("carg", []):
        for agr in cargo.get("agr", []):
            for par in agr.get("par", []):
                for c in par.get("cand", []):
                    lista.append({
                        "numero":    c.get("n", ""),
                        "nome":      c.get("nm", ""),
                        "nome_urna": c.get("nmu", "") or c.get("nm", ""),
                        "partido":   par.get("sg", ""),
                        "votos_pe":  int(c.get("vap", 0)),
                    })
    lista.sort(key=lambda x: x["votos_pe"], reverse=True)
    return lista

def votos_do_candidato_em(dados_municipio, nome_busca):
    for cargo in dados_municipio.get("carg", []):
        for agr in cargo.get("agr", []):
            for par in agr.get("par", []):
                for c in par.get("cand", []):
                    if nome_busca.lower() in c.get("nm", "").lower():
                        return int(c.get("vap", 0))
    return 0

# --- GitHub commit ---
def git_commit_csv(caminho_local, caminho_repo, mensagem):
    if not GITHUB_OK:
        print("[git] Variáveis GITHUB_* não configuradas — apenas local")
        return False

    with open(caminho_local, "rb") as f:
        conteudo_b64 = base64.b64encode(f.read()).decode("ascii")

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{caminho_repo}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    sha = None
    try:
        r = requests.get(url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception as e:
        print(f"[git] Erro ao consultar SHA: {e}")

    payload = {
        "message": mensagem,
        "content": conteudo_b64,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(url, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            print(f"[git] ✅ Commitado: {caminho_repo}")
            return True
        print(f"[git] ❌ Falha ({r.status_code}): {r.text[:300]}")
        return False
    except Exception as e:
        print(f"[git] ❌ Exceção: {e}")
        return False

# --- Rotas ---
@app.route("/")
def index():
    return send_file(os.path.join(PASTA, "mapa_votos.html"))

@app.route("/api/status")
def status():
    return jsonify({
        "github_configurado": GITHUB_OK,
        "owner": GITHUB_OWNER,
        "repo": GITHUB_REPO,
        "branch": GITHUB_BRANCH,
        "candidatos_locais": len([f for f in os.listdir(PASTA_DADOS) if f.endswith(".csv")]),
        "jobs_ativos": sum(1 for j in JOBS.values() if j.get("status") not in ("ok","erro")),
    })

@app.route("/api/cargos")
def listar_cargos():
    return jsonify([{"id": k, "nome": v["nome"]} for k, v in CARGOS.items()])

@app.route("/api/candidatos")
def listar_candidatos():
    cargo_filtro = request.args.get("cargo")
    arquivos = sorted(f for f in os.listdir(PASTA_DADOS) if f.endswith(".csv"))
    resultado = []
    for arq in arquivos:
        cand_id_raw = arq[:-4]
        cargo_key, cand_id = parse_id(cand_id_raw)
        if cargo_filtro and cargo_key != cargo_filtro:
            continue
        caminho = os.path.join(PASTA_DADOS, arq)
        try:
            df = pd.read_csv(caminho)
            col = df.columns[-1]
            mask_total = df["municipio"].str.upper() == "TOTAL"
            total = int(df.loc[mask_total, col].sum()) if mask_total.any() else int(df[col].sum())
            resultado.append({
                "id": cand_id, "nome": col, "total": total,
                "cargo": cargo_key, "cargo_nome": CARGOS[cargo_key]["nome"],
            })
        except Exception as e:
            resultado.append({
                "id": cand_id, "nome": cand_id, "total": 0,
                "cargo": cargo_key, "cargo_nome": CARGOS[cargo_key]["nome"],
                "erro": str(e),
            })
    return jsonify(resultado)

@app.route("/api/votos/<cand_id>")
def obter_votos(cand_id):
    _, cand_id_norm = parse_id(cand_id)
    caminho = os.path.join(PASTA_DADOS, f"{cand_id_norm}.csv")
    if not os.path.exists(caminho):
        return jsonify({"erro": "não encontrado"}), 404
    df = pd.read_csv(caminho)
    col = df.columns[-1]
    votos_map = {}
    for _, row in df.iterrows():
        mun = str(row["municipio"]).strip()
        if mun.upper() == "TOTAL":
            continue
        try:
            votos_map[mun] = int(row[col])
        except (ValueError, TypeError):
            votos_map[mun] = 0
    cargo_key, _ = parse_id(cand_id_norm)
    return jsonify({
        "candidato": col,
        "id": cand_id_norm,
        "cargo": cargo_key,
        "cargo_nome": CARGOS[cargo_key]["nome"],
        "total": sum(votos_map.values()),
        "votos": votos_map,
    })

@app.route("/api/buscar")
def buscar():
    q = request.args.get("q", "").strip().lower()
    cargo_key = request.args.get("cargo", "ESTADUAL").upper()
    if cargo_key not in CARGOS or not q:
        return jsonify([])
    cands = candidatos_pe(cargo_key)
    if q.isdigit():
        found = [c for c in cands if c["numero"] == q]
    else:
        found = [c for c in cands if q in c["nome"].lower() or q in c["nome_urna"].lower()]
    for c in found:
        c["cargo"] = cargo_key
        c["cargo_nome"] = CARGOS[cargo_key]["nome"]
    return jsonify(found[:30])

# --- Importação assíncrona ---

def _processar_importacao(job_id, nome, cargo_key, cand_id, caminho_saida, caminho_repo):
    try:
        municipios = municipios_pe()
        total_mun = len(municipios)

        with JOBS_LOCK:
            JOBS[job_id]["total"] = total_mun

        def baixar_um(mun):
            cod_mun = mun["cd"]
            nome_mun = mun["nm"]
            url = url_votos(cargo_key, cod_mun)
            try:
                dados = baixar_json(url, timeout=15)
                votos = votos_do_candidato_em(dados, nome)
                return (nome_mun.upper(), votos)
            except Exception:
                return (nome_mun.upper(), 0)

        resultados = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futuros = [ex.submit(baixar_um, m) for m in municipios]
            for i, f in enumerate(as_completed(futuros), 1):
                resultados.append(f.result())
                with JOBS_LOCK:
                    JOBS[job_id]["progresso"] = i

        resultados.sort(key=lambda x: x[0])
        total = sum(v for _, v in resultados)
        registros = [{"municipio": m, nome: v} for m, v in resultados]
        registros.append({"municipio": "TOTAL", nome: total})

        df = pd.DataFrame(registros)
        df.to_csv(caminho_saida, index=False, encoding="utf-8-sig")

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "commitando"
            JOBS[job_id]["votos_total"] = total

        # Commit no GitHub (bloqueia até terminar, mas o usuário já vê "ok")
        if GITHUB_OK:
            git_commit_csv(
                caminho_local=caminho_saida,
                caminho_repo=caminho_repo,
                mensagem=f"Adiciona CSV: {nome} ({CARGOS[cargo_key]['nome']})"
            )

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "ok"
            JOBS[job_id]["mensagem"] = f"Salvo com sucesso ({total:,} votos)"

    except Exception as e:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "erro"
            JOBS[job_id]["mensagem"] = str(e)


@app.route("/api/importar", methods=["POST"])
def importar():
    data = request.get_json() or {}
    nome = (data.get("nome") or "").strip()
    cargo_key = (data.get("cargo") or "ESTADUAL").upper()
    if not nome:
        return jsonify({"erro": "informe o nome do candidato"}), 400
    if cargo_key not in CARGOS:
        return jsonify({"erro": f"cargo inválido: {cargo_key}"}), 400

    cand_id = id_arquivo(cargo_key, nome)
    caminho_saida = os.path.join(PASTA_DADOS, f"{cand_id}.csv")
    caminho_repo = f"dados/{cand_id}.csv"

    if os.path.exists(caminho_saida):
        return jsonify({"ok": True, "id": cand_id, "nome": nome,
                        "cargo": cargo_key, "ja_existia": True,
                        "mensagem": "Candidato já está na base"})

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "baixando",
            "progresso": 0,
            "total": 0,
            "nome": nome,
            "cargo": cargo_key,
            "candidato_id": cand_id,
            "votos_total": 0,
            "mensagem": "",
        }

    t = threading.Thread(
        target=_processar_importacao,
        args=(job_id, nome, cargo_key, cand_id, caminho_saida, caminho_repo),
        daemon=True,
    )
    t.start()

    return jsonify({
        "ok": True,
        "job_id": job_id,
        "nome": nome,
        "candidato_id": cand_id,
        "cargo": cargo_key,
        "github_configurado": GITHUB_OK,
    }), 202


@app.route("/api/importar/status/<job_id>")
def importar_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"erro": "job não encontrado"}), 404
        return jsonify(job)


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    print(f"Servidor rodando em http://0.0.0.0:{porta}")
    print(f"GitHub configurado: {GITHUB_OK}")
    app.run(host="0.0.0.0", port=porta, debug=False, threaded=True)