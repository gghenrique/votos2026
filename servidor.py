from flask import Flask, jsonify, send_file, request
from flask_cors import CORS
import os, re, json, base64, unicodedata
import pandas as pd
import requests

PASTA       = os.path.dirname(os.path.abspath(__file__))
PASTA_DADOS = os.path.join(PASTA, "dados")
os.makedirs(PASTA_DADOS, exist_ok=True)

app = Flask(__name__, static_folder=PASTA)
CORS(app)

# --- TSE ---
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

def url_estado(cargo_key: str) -> str:
    c = CARGOS[cargo_key]["codigo"]
    return (f"https://resultados.tse.jus.br/oficial/ele2026/{COD_ELEICAO}/dados/"
            f"{UF}/{UF}-c{c}-e{COD_ELEICAO_6}-u.json")

def url_votos(cargo_key: str, cod_mun: str) -> str:
    c = CARGOS[cargo_key]["codigo"]
    return (f"https://resultados.tse.jus.br/oficial/ele2026/{COD_ELEICAO}/dados/"
            f"{UF}/{UF}{cod_mun}-c{c}-e{COD_ELEICAO_6}-u.json")

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

def id_arquivo(cargo_key: str, nome: str) -> str:
    return f"{cargo_key}_{slugify(nome)}"

def parse_id(id_str: str):
    """Recebe 'ESTADUAL_LUCIANO_DUQUE' ou só 'NOME' e devolve (cargo, id_normalizado)."""
    for key in CARGOS:
        if id_str.startswith(f"{key}_"):
            return key, id_str
    # fallback: arquivos antigos → ESTADUAL
    return "ESTADUAL", f"ESTADUAL_{id_str}"

# --- Caches ---
_cache = {"cadastro": None, "estado": {k: None for k in CARGOS}}

def get_cadastro():
    if _cache["cadastro"] is None:
        _cache["cadastro"] = baixar_json(URL_CADASTRO, timeout=60)
    return _cache["cadastro"]

def get_estado(cargo_key: str):
    if _cache["estado"][cargo_key] is None:
        _cache["estado"][cargo_key] = baixar_json(url_estado(cargo_key), timeout=60)
    return _cache["estado"][cargo_key]

def municipios_pe():
    for abr in get_cadastro().get("abr", []):
        if abr.get("cd") == UF:
            return abr.get("mu", [])
    return []

def candidatos_pe(cargo_key: str):
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

# --- Rotas ---
@app.route("/")
def index():
    return send_file(os.path.join(PASTA, "mapa_votos.html"))

@app.route("/api/cargos")
def listar_cargos():
    return jsonify([{"id": k, "nome": v["nome"]} for k, v in CARGOS.items()])

@app.route("/api/candidatos")
def listar_candidatos():
    cargo_filtro = request.args.get("cargo")  # opcional
    arquivos = sorted(f for f in os.listdir(PASTA_DADOS) if f.endswith(".csv"))
    resultado = []
    for arq in arquivos:
        cand_id = arq[:-4]
        cargo_key, cand_id = parse_id(cand_id)
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
    # cand_id pode vir sem prefixo (compat) ou com prefixo
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
    if cargo_key not in CARGOS:
        return jsonify([])
    if not q:
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

    if os.path.exists(caminho_saida):
        return jsonify({"ok": True, "id": cand_id, "nome": nome,
                        "cargo": cargo_key, "ja_existia": True})

    municipios = municipios_pe()
    registros = []
    total = 0
    for mun in municipios:
        cod_mun = mun["cd"]
        nome_mun = mun["nm"]
        url = url_votos(cargo_key, cod_mun)
        try:
            dados = baixar_json(url, timeout=20)
            votos = votos_do_candidato_em(dados, nome)
            registros.append({"municipio": nome_mun.upper(), nome: votos})
            total += votos
        except Exception:
            registros.append({"municipio": nome_mun.upper(), nome: 0})

    registros.append({"municipio": "TOTAL", nome: total})
    df = pd.DataFrame(registros)
    df.to_csv(caminho_saida, index=False, encoding="utf-8-sig")

    return jsonify({"ok": True, "id": cand_id, "nome": nome,
                    "cargo": cargo_key, "total": total})

if __name__ == "__main__":
    import os
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=False)