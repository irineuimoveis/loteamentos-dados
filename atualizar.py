"""Robô que baixa os lotes do forinvest e gera o data.json do mapa.
Roda sozinho no GitHub Actions. Usuário e senha ficam nos Secrets do GitHub."""
import base64, gzip, hashlib, io, json, math, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://irineuimoveis-be.forinvest.com.br/api/"
HEAD = {"Company": "irineuimoveis", "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9", "Origin": "https://irineuimoveis.forinvest.com.br",
        "Referer": "https://irineuimoveis.forinvest.com.br/"}

ULTIMO = [0.0]
INTERVALO = 2.0          # segundos mínimos entre dois pedidos ao forinvest (nunca "metralha" o servidor)

def via_ponte(url, metodo, h, dados, espera):
    """Pede para a ponte do Google (Apps Script) buscar no forinvest: o pedido sai dos servidores do Google."""
    pac = {"k": os.environ.get("PONTE_CHAVE", ""), "url": url, "metodo": metodo,
           "auth": h.get("Authorization", ""), "corpo": dados.decode() if dados else ""}
    r = urllib.request.Request(os.environ["PONTE_URL"], data=json.dumps(pac).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=espera) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    if res.get("erro"): raise RuntimeError("Ponte: " + res["erro"])
    code = int(res.get("code") or 0)
    if code >= 400:
        import email.message; hd = email.message.Message()
        if res.get("retryAfter"): hd["Retry-After"] = str(res["retryAfter"])
        raise urllib.error.HTTPError(url, code, "via ponte", hd, io.BytesIO((res.get("corpo") or "").encode()))
    return json.loads(res.get("corpo") or "null")

def chamar(url, corpo=None, token=None, espera=120):
    h = dict(HEAD)
    if token: h["Authorization"] = "Bearer " + token
    dados = json.dumps(corpo).encode() if corpo is not None else None
    falta = INTERVALO - (time.time() - ULTIMO[0])
    if falta > 0: time.sleep(falta)
    ULTIMO[0] = time.time()
    try:
        if os.environ.get("PONTE_URL") and url.startswith(BASE):
            return via_ponte(url, "post" if dados else "get", h, dados, espera)
        r = urllib.request.Request(url, data=dados, headers=h, method="POST" if dados else "GET")
        with urllib.request.urlopen(r, timeout=espera) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401 and token: RECUSADO[0] = True     # token guardado deixou de valer: na próxima rodada faz login de novo
        raise

RECUSADO = [False]

def achar_token(o):
    if isinstance(o, str):
        m = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", o)
        return m.group(0) if m else None
    if isinstance(o, dict): o = list(o.values())
    if isinstance(o, list):
        for v in o:
            t = achar_token(v)
            if t: return t
    return None

def rm(s):
    try: return json.loads(gzip.decompress(base64.b64decode(s)).decode())
    except Exception: return {}

def ler_entrada(v):
    """Cod. do forinvest → entrada em reais. 16.0 = R$ 16.000"""
    t = re.sub(r"[^\d.,]", "", str(v or ""))
    if not t: return None
    if "," in t: t = t.replace(".", "").replace(",", ".")
    try: n = float(t)
    except ValueError: return None
    if n <= 0: return None
    return round(n * 1000) if n < 1000 else round(n)

def achar_dict(o, chave):
    """Acha o primeiro bloco da resposta que tenha essa informação preenchida."""
    if isinstance(o, dict):
        if o.get(chave) not in (None, ""): return o
        o = list(o.values())
    if isinstance(o, list):
        for v in o:
            r = achar_dict(v, chave)
            if r: return r
    return None

def indice_unidades(o, idx):
    """Percorre a resposta inteira e guarda coordenadas e Cod. de cada unidade pelo id.
    Alguns lotes (vendidos/reservados) só trazem a localização em partes aninhadas."""
    if isinstance(o, dict):
        uid = o.get("id")
        if uid and o.get("unitNumber") is not None:
            atual = idx.setdefault(uid, {})
            for k in ("latitude", "longitude", "internalCode"):
                if o.get(k) not in (None, "") and not atual.get(k): atual[k] = o[k]
            if not atual.get("internalCode") and o.get("rmCompressedData"):
                cod = rm(o["rmCompressedData"]).get("InternalCode")
                if cod not in (None, ""): atual["internalCode"] = cod
        for v in o.values(): indice_unidades(v, idx)
    elif isinstance(o, list):
        for v in o: indice_unidades(v, idx)
    return idx

# Página de cada loteamento (/loteamentos/<id>/): só carrega o site principal, então nunca fica desatualizada
CASCA = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover"><title>Mapa dos lotes | Irineu Imóveis</title><style>body{margin:0;background:#1d2b1c}</style></head><body><script>
fetch('../?v=' + Date.now(), {cache: 'no-store'}).then(function(r){ return r.text(); }).then(function(t){ document.open(); document.write(t); document.close(); })
  .catch(function(){ location.href = '../?l=' + location.pathname.split('/').filter(Boolean).pop() + location.search.replace('?', '&'); });
</script></body></html>
"""

LADO_MAX = 4096   # maior lado da planta em pixels (limite seguro para celulares)

Z_MIN, Z_MAX = 13, 19   # níveis de zoom em que a planta aparece em pedaços
BLOCO = 1024            # cada arquivo cobre 4x4 quadradinhos do mapa: 16 vezes menos arquivos (o GitHub Pages publica sem travar)

def _px(lat, lng, z):
    """Coordenada em pixels do mapa (Web Mercator) no zoom z."""
    n = 256 * 2 ** z
    s = math.sin(math.radians(lat))
    return (lng + 180) / 360 * n, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n

def gerar_ladrilhos(img, limites, pasta, ver):
    """Fatia a planta em quadradinhos de 256 px por nível de zoom (igual ao Google Maps).
    O celular só baixa os pedaços que estão na tela, no tamanho certo para o zoom: fica leve e nítido."""
    from PIL import Image
    destino = f"{pasta}/{ver}"
    if os.path.isdir(destino) and any(os.scandir(destino)): return
    if os.path.isdir(pasta):
        for d in os.listdir(pasta):
            if d != ver: shutil.rmtree(os.path.join(pasta, d), ignore_errors=True)
    lado = 8192
    if max(img.size) > lado:
        k = lado / max(img.size); img = img.resize((round(img.size[0] * k), round(img.size[1] * k)), Image.LANCZOS)
    rgba = img.convert("RGBA")
    W, H = rgba.size
    (s, w), (n, e) = limites
    total = 0
    for z in range(Z_MIN, Z_MAX + 1):
        x0, y0 = _px(n, w, z); x1, y1 = _px(s, e, z)
        sx, sy = (x1 - x0) / W, (y1 - y0) / H          # pixels do mapa por pixel da imagem
        B = BLOCO
        for tx in range(int(x0 // B), int(x1 // B) + 1):
            for ty in range(int(y0 // B), int(y1 // B) + 1):
                # área do bloco na imagem
                ax0, ay0 = (tx * B - x0) / sx, (ty * B - y0) / sy
                ax1, ay1 = ((tx + 1) * B - x0) / sx, ((ty + 1) * B - y0) / sy
                cx0, cy0, cx1, cy1 = max(0, ax0), max(0, ay0), min(W, ax1), min(H, ay1)
                if cx1 <= cx0 or cy1 <= cy0: continue
                # onde esse recorte cai dentro do ladrilho
                dx0, dy0 = round((cx0 - ax0) * sx), round((cy0 - ay0) * sy)
                dw, dh = max(1, round((cx1 - cx0) * sx)), max(1, round((cy1 - cy0) * sy))
                pedaco = rgba.resize((min(dw, B), min(dh, B)), Image.LANCZOS, box=(cx0, cy0, cx1, cy1))
                if pedaco.getchannel("A").getextrema()[1] == 0: continue
                lad = Image.new("RGBA", (B, B), (0, 0, 0, 0)); lad.paste(pedaco, (dx0, dy0))
                os.makedirs(f"{destino}/{z}", exist_ok=True)
                lad.save(f"{destino}/{z}/{tx}_{ty}.webp", "WEBP", quality=86, method=4, alpha_quality=90)
                total += 1
    print(f"  {total} pedaços de planta gerados em {destino}")

PL = "_plantas"   # pasta local do ramo "plantas" (as plantas ficam fora do site, então o GitHub Pages publica rápido)
REPO = os.environ.get("SITE_REPO") or os.environ.get("GITHUB_REPOSITORY", "irineuimoveis/loteamentos")   # repositório do site (privado)
# Lotes, plantas e reservas vão para um repositório PÚBLICO separado (o do site pode ser privado)
DADOS_REPO = os.environ.get("DADOS_REPO") or REPO
RAW_PL = f"https://raw.githubusercontent.com/{DADOS_REPO}/plantas/"
_REMOTO = [None]

def remoto():
    """Nome do "remote" do git onde ficam os ramos dados e plantas."""
    if _REMOTO[0]: return _REMOTO[0]
    gh = os.environ.get("GH_TOKEN")          # o checkout é feito sem guardar a chave: o repositório do site usa a do Actions
    if gh and os.environ.get("GITHUB_ACTIONS") == "true":
        git("remote", "set-url", "origin", f"https://x-access-token:{gh}@github.com/{REPO}.git", ok=True)
    tok = os.environ.get("DADOS_TOKEN")
    if os.environ.get("DADOS_REPO") and tok and os.environ.get("DADOS_REPO") != REPO:
        url = f"https://x-access-token:{tok}@github.com/{os.environ['DADOS_REPO']}.git"
        if git("remote", "get-url", "pub", ok=True).returncode == 0: git("remote", "set-url", "pub", url)
        else: git("remote", "add", "pub", url)
        _REMOTO[0] = "pub"
    else:
        _REMOTO[0] = "origin"
    return _REMOTO[0]

def git_pl(*args, **kw):
    """git trabalhando na pasta das plantas, com índice próprio (não mexe no site)."""
    env = dict(os.environ, GIT_INDEX_FILE=os.path.abspath(".git/indice-plantas"))
    return subprocess.run(["git", "--git-dir=" + os.path.abspath(".git"), "--work-tree=.", *args], cwd=PL, env=env,
                          capture_output=True, text=True, **kw)

def preparar_plantas():
    os.makedirs(PL, exist_ok=True)
    if os.environ.get("GITHUB_ACTIONS") != "true": return
    if git("fetch", "-q", "--depth", "1", remoto(), "plantas", ok=True).returncode == 0:
        git_pl("checkout", "FETCH_HEAD", "--", ".")
        print("Plantas já cortadas carregadas do ramo plantas.")

def publicar_plantas():
    """Grava as plantas no ramo "plantas" (só a versão atual, sem histórico: o repositório não cresce)."""
    if os.environ.get("GITHUB_ACTIONS") != "true": return
    idx = os.path.abspath(".git/indice-plantas")
    if os.path.exists(idx): os.remove(idx)
    git_pl("add", "-A", ".", check=True)
    arvore = git_pl("write-tree", check=True).stdout.strip()
    if git("ls-remote", remoto(), "plantas", ok=True).stdout.strip():
        git("fetch", "-q", "--depth", "1", remoto(), "plantas", ok=True)
        if git("rev-parse", "FETCH_HEAD^{tree}", ok=True).stdout.strip() == arvore:
            print("Plantas sem mudança."); return
    commit = git("commit-tree", arvore, "-m", "Plantas").stdout.strip()
    git("push", "-q", "--force", remoto(), f"{commit}:refs/heads/plantas")
    print("Plantas publicadas no ramo plantas.")

def otimizar_planta(lot):
    """Baixa a planta do forinvest uma vez, corta em blocos e guarda no ramo "plantas".
    Só refaz quando a planta original muda. Se algo falhar, continua usando a original."""
    p = lot.get("planta")
    if not p or not p.get("url"): return
    src = p["url"]
    ver = hashlib.sha1((src + f"|bloco{BLOCO}").encode()).hexdigest()[:10]
    pasta = re.sub(r"[^a-z0-9-]", "", lot["id"].lower()) or "lot"
    os.makedirs(PL, exist_ok=True)
    meta = f"{PL}/{pasta}.json"
    try:
        info = json.load(open(meta, encoding="utf-8")) if os.path.exists(meta) else {}
        if info.get("src") != src or info.get("ladrilhos") != f"{pasta}/{ver}" or not os.path.isdir(f"{PL}/{pasta}/{ver}") \
           or not (info.get("usar_original") or os.path.exists(f"{PL}/{info.get('arquivo', '-')}")):
            from PIL import Image
            req = urllib.request.Request(src, headers={"User-Agent": HEAD["User-Agent"]})
            bruto = urllib.request.urlopen(req, timeout=120).read()
            img = Image.open(io.BytesIO(bruto))
            img = img.convert("RGBA" if ("A" in img.getbands() or img.mode == "P") else "RGB")
            w, h = img.size
            gerar_ladrilhos(img, p["limites"], f"{PL}/{pasta}", ver)
            k = min(1.0, LADO_MAX / max(w, h))
            if k < 1: img = img.resize((round(w * k), round(h * k)), Image.LANCZOS)
            opcoes = []
            b1 = io.BytesIO(); img.save(b1, "WEBP", quality=90, method=6, alpha_quality=95); opcoes.append(("webp", b1.getvalue()))
            b2 = io.BytesIO(); img.save(b2, "PNG", optimize=True); opcoes.append(("png", b2.getvalue()))
            ext, novo = min(opcoes, key=lambda o: len(o[1]))
            for velho in ("webp", "png"):
                if os.path.exists(f"{PL}/{pasta}.{velho}"): os.remove(f"{PL}/{pasta}.{velho}")
            if len(novo) >= len(bruto) and k == 1:
                info = {"src": src, "usar_original": True, "ladrilhos": f"{pasta}/{ver}"}
            else:
                open(f"{PL}/{pasta}.{ext}", "wb").write(novo)
                info = {"src": src, "arquivo": f"{pasta}.{ext}", "ladrilhos": f"{pasta}/{ver}",
                        "original_kb": len(bruto) // 1024, "otimizada_kb": len(novo) // 1024}
            json.dump(info, open(meta, "w", encoding="utf-8"))
            print(f"  planta {pasta}: {len(bruto)//1024} KB -> {len(novo)//1024} KB")
        if os.path.isdir(f"{PL}/{info['ladrilhos']}"):
            p["ladrilhos"] = {"base": info["ladrilhos"], "zmin": Z_MIN, "zmax": Z_MAX, "bloco": BLOCO, "ramo": "plantas"}
        if not info.get("usar_original"):
            p["original"] = src
            p["url"] = f"{RAW_PL}{info['arquivo']}?v={ver}"
    except Exception as e:
        print("  AVISO: não consegui otimizar a planta de", lot["id"], "-", e)

def ler_deslocamentos():
    """Ajuste de posição de cada loteamento salvo na Área do corretor (config.json): metros para norte/leste."""
    try:
        cfg = json.load(open("config.json", encoding="utf-8"))
        out = {}
        for lid, v in (cfg.get("lots") or {}).items():
            d = v.get("desloc") or {}
            n, e = float(d.get("n") or 0), float(d.get("e") or 0)
            if n or e: out[lid] = {"n": n, "e": e}
        return out
    except Exception:
        return {}

def deslocar(lot, d):
    """Move lotes, planta e centro do loteamento d['n'] metros para o norte e d['e'] metros para o leste."""
    if not d: return
    lat0 = lot["centro"][0]
    dla, dlo = d["n"] / 111320.0, d["e"] / (111320.0 * math.cos(math.radians(lat0)))
    for l in lot["lotes"]: l[4] = round(l[4] + dla, 7); l[5] = round(l[5] + dlo, 7)
    lot["centro"] = [lot["centro"][0] + dla, lot["centro"][1] + dlo]
    if lot.get("planta"):
        (s_, w), (n_, e) = lot["planta"]["limites"]
        lot["planta"]["limites"] = [[s_ + dla, w + dlo], [n_ + dla, e + dlo]]
    lot["desloc"] = d

RESERVAS_TMP = {}
_CAMPOS_VISTOS = [False]

TOKEN = [None]
UNIDS_TMP = {}

def quem_reservou(r, u):
    """Nome do CORRETOR que reservou (o "Reservado por" do card do forinvest). Nunca o nome do cliente."""
    if not _CAMPOS_VISTOS[0]:
        _CAMPOS_VISTOS[0] = True
        print("  campos da reserva no forinvest:", ", ".join(sorted(k for k, v in r.items() if v not in (None, "", [], {}))))
    for v in (r.get("reservedByName"), r.get("userName"), r.get("brokerName"), r.get("createdByName"),
              (r.get("user") or {}).get("name") if isinstance(r.get("user"), dict) else None,
              (r.get("createdBy") or {}).get("name") if isinstance(r.get("createdBy"), dict) else None,
              u.get("reservedBy") if isinstance(u.get("reservedBy"), str) else None):
        if isinstance(v, str) and v.strip(): return v.strip()
    return None

def salvar_reservas():
    """Nomes e datas das reservas: guardados cifrados (AES) com a senha do forinvest.
    O arquivo fica público no GitHub, mas só abre no painel do corretor, com essa senha."""
    senha = os.environ.get("FORINVEST_SENHA") or os.environ.get("FORINVEST_ADMIN_SENHA", "")
    if not senha: return
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        if subprocess.run([sys.executable, "-m", "pip", "install", "-q", "cryptography"]).returncode:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--break-system-packages", "cryptography"])
        try: from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError: print("  AVISO: sem a biblioteca de criptografia; reservas não salvas"); return
    texto = json.dumps(RESERVAS_TMP, ensure_ascii=False, sort_keys=True).encode()
    chave = hashlib.pbkdf2_hmac("sha256", senha.encode(), b"irineu-reservas-v1", 150000, 32)
    marca = hashlib.sha256(chave + texto).hexdigest()[:16]
    try:
        if json.load(open("reservas.json", encoding="utf-8")).get("h") == marca: return
    except Exception: pass
    nonce = os.urandom(12)
    dados = base64.b64encode(nonce + AESGCM(chave).encrypt(nonce, texto, None)).decode()
    json.dump({"v": 1, "h": marca, "dados": dados}, open("reservas.json", "w", encoding="utf-8"))
    print("  reservas atualizadas:", sum(len(v) for v in RESERVAS_TMP.values()))

def agora_br():
    """Hora de Brasília sem fuso (as datas do forinvest vêm assim)."""
    return datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)

def vencimentos(lotes_por_id, reservas):
    """Para o site: quando vence cada reserva amarela {loteamento: {posição do lote: data/hora}}."""
    out = {}
    for lid, rs in (reservas or {}).items():
        L = lotes_por_id.get(lid)
        if not L: continue
        pos = {f"{l[0]}|{l[1]}": i for i, l in enumerate(L["lotes"])}
        for k, v in rs.items():
            i = pos.get(k)
            if i is None or L["lotes"][i][3] != 2: continue
            ate = v.get("ate") or (v.get("desde") and (datetime.fromisoformat(v["desde"][:19]) + timedelta(hours=48)).isoformat())
            if ate: out.setdefault(lid, {})[str(i)] = ate[:19]
    return out

def montar(offer, item):
    unidades = offer["units"]
    idx = indice_unidades(offer, {})
    of = achar_dict(offer, "zoomMap") or achar_dict(offer, "imagemLocalizacaoN")
    info = rm(of.get("rmCompressedData")) if of else {}
    planos, lotes, sem_local = [], [], []
    for u in unidades:
        tp = u["tabPrices"][-1] if u.get("tabPrices") else None  # vale a última linha da tabela
        info_u = idx.get(u["id"], {})
        try: lat, lng = float(info_u.get("latitude")), float(info_u.get("longitude"))
        except (TypeError, ValueError): lat = lng = 0
        if abs(lat) < 1 or abs(lng) < 1:
            sem_local.append(u.get("plantDescription", "").strip() + " lote " + str(u.get("unitNumber"))); continue
        pi = None
        if tp:
            fases = re.findall(r"(\d+)\s*X\s*(\d+(?:\s+\d+/\d+|/\d+)?)", tp.get("description") or "", re.I)
            if fases:
                k = " + ".join(n + " X " + f.strip() for n, f in fases)
                if k not in planos: planos.append(k)
                pi = planos.index(k)
        q = re.sub(r"^QUADRA\s+", "", (u.get("plantDescription") or "").strip(), flags=re.I)
        try: num = str(int(u["unitNumber"]))
        except Exception: num = str(u.get("unitNumber"))
        entrada = ler_entrada(info_u.get("internalCode"))
        st = u.get("status")
        try: st = int(st)
        except (TypeError, ValueError): st = None
        ativas = [r for r in (u.get("reservations") or []) if r.get("isActive")]
        if st == 0 and ativas: st = 2          # reserva ativa no forinvest = reservado
        if st == 2:
            # a reserva vale 48 horas a partir de quando foi feita: passou disso, o lote volta a ficar disponível
            feitas = [x.get("createdAt") for x in ativas if x.get("createdAt")]
            try:
                if feitas and datetime.fromisoformat(max(feitas)[:19]) + timedelta(hours=48) < agora_br(): st, ativas = 0, []
            except Exception: pass
        if st in (2, 4) and ativas:
            r = max(ativas, key=lambda x: x.get("createdAt") or "")
            ate = None
            if r.get("createdAt") and st == 2:
                try: ate = (datetime.fromisoformat(r["createdAt"][:19]) + timedelta(hours=48)).isoformat()
                except Exception: ate = None
            RESERVAS_TMP.setdefault(item["id"], {})[f"{q}|{num}"] = {"nome": quem_reservou(r, u),
                                                                    "desde": (r.get("createdAt") or "")[:19] or None, "ate": ate}
        lotes.append([q, num, u.get("area") or 0, st, round(float(lat), 7), round(float(lng), 7), pi, entrada or None])
        UNIDS_TMP[u["id"]] = [item["id"], f"{q}|{num}"]
    if sem_local: print("  AVISO:", len(sem_local), "lotes sem localização no forinvest:", ", ".join(sem_local[:20]))
    lotes.sort(key=lambda l: (l[0], int(l[1]) if l[1].isdigit() else 0))
    nome = item.get("nome") or (info.get("Name") or "Loteamento").title()
    cidade = item.get("cidade") or ((info.get("City") or "") + ("/" + info["UF"] if info.get("UF") else ""))
    planta = None
    if of and offer.get("backgroundMapsImage") and of.get("imagemLocalizacaoN"):
        planta = {"url": offer["backgroundMapsImage"],
                  "limites": [[float(of["imagemLocalizacaoS"]), float(of["imagemLocalizacaoW"])],
                              [float(of["imagemLocalizacaoN"]), float(of["imagemLocalizacaoE"])]]}
    if of and of.get("latitude"):
        centro = [float(of["latitude"]), float(of["longitude"])]
    else:
        centro = [sum(l[4] for l in lotes) / len(lotes), sum(l[5] for l in lotes) / len(lotes)]
    bairro = item.get("bairro")
    if not bairro:
        for chave in ("neighborhood", "district", "bairro", "Neighborhood", "Bairro"):
            d = achar_dict(offer, chave)
            if d and isinstance(d.get(chave), str) and d[chave].strip(): bairro = d[chave].strip().title(); break
    return {"id": item["id"], "nome": nome, "cidade": cidade, "bairro": bairro, "centro": centro,
            "zoom": int(of.get("zoomMap") or 17) if of else 17,
            "planta": planta, "planos": planos, "lotes": lotes}

DESLOC = {}

def baixar_todos(lista, token):
    """Baixa os loteamentos UM DE CADA VEZ, com intervalo: o forinvest passou a limitar pedidos seguidos (erro 429).
    Se ele mandar esperar, espera e tenta de novo; se continuar barrando, para a rodada sem dar erro."""
    saida, barrado = [], 0
    for n, item in enumerate(lista):
        if barrado >= 2:                                   # bloqueado de vez: deixa o resto para a próxima rodada
            saida.append((item, None, Pausa("429"))); continue
        for tentativa in range(3):
            try:
                saida.append((item, chamar(BASE + "Offer/" + item["offerId"] + "?loadUnits=true", token=token), None)); barrado = 0; break
            except urllib.error.HTTPError as e:
                if e.code != 429 or tentativa == 2:
                    if e.code == 429: barrado += 1
                    saida.append((item, None, e)); break
                ra = e.headers.get("Retry-After")
                if ra and ra.replace(".", "").isdigit() and float(ra) > 120:      # mandou esperar muito: não insiste, para tudo
                    RETRY[0] = float(ra); barrado = 99; saida.append((item, None, e)); break
                if n == 0 and tentativa == 0: print("  forinvest pediu para esperar | Retry-After:", ra or "-", "|", (e.read() or b"")[:200].decode("utf-8", "replace"))
                time.sleep(min(60, float(ra)) if ra and ra.replace(".", "").isdigit() else 10 * (tentativa + 1))
            except Exception as e:
                saida.append((item, None, e)); break
        time.sleep(1.5)                                    # intervalo entre um loteamento e outro
    return saida

MARCA = [None]
ultimos = [[]]
ST_RM = {"100": 0, "903": 2}      # códigos do RM no log: 100 = vago (disponível), 903 = reserva 48 horas (reservado)

def ler_do_ramo(arquivo):
    try: return json.loads(git("show", "FETCH_HEAD:" + arquivo).stdout)
    except Exception: return None

def status_anterior():
    try:
        git("fetch", "-q", "--depth", "1", remoto(), "dados")
        return ler_do_ramo("status.json") or {}
    except Exception:
        return {}

def garantir_cripto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM; return True
    except ImportError:
        for extra in ([], ["--break-system-packages"]):
            if subprocess.run([sys.executable, "-m", "pip", "install", "-q", *extra, "cryptography"]).returncode == 0: break
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM; return True
        except ImportError:
            return False

def chave_reservas():
    senha = os.environ.get("FORINVEST_SENHA") or os.environ.get("FORINVEST_ADMIN_SENHA", "")
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), b"irineu-reservas-v1", 150000, 32) if senha else None

def abrir_reservas(arq):
    """Abre o reservas.json da rodada anterior (cifrado com a senha do forinvest)."""
    if not arq: return {}
    garantir_cripto()
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        bruto = base64.b64decode(arq["dados"]); chave = chave_reservas()
        return json.loads(AESGCM(chave).decrypt(bruto[:12], bruto[12:], None))
    except Exception as e:
        print("  AVISO: não consegui abrir as reservas anteriores:", e)
        return None

def nome_pelo_email(email):
    """isaque.tonolli@associadoirineu.com.br -> Isaque Tonolli"""
    if not email or "@" not in email: return None
    partes = [p for p in re.split(r"[._\-]+", email.split("@")[0]) if p and not p.isdigit()]
    return " ".join(p.capitalize() for p in partes) or None

# ===== Sessão guardada: o forinvest passou a barrar logins repetidos (erro 429). O robô roda a cada 5 min,
# então guarda o token (cifrado com a senha, no ramo "dados") e só faz login de novo quando ele estiver vencendo. =====
SESSAO = [None]
class Pausa(Exception): pass

def jwt_exp(tok):
    try:
        p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
        return float(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:
        return 0

def ler_sessao():
    if SESSAO[0] is not None: return SESSAO[0]
    SESSAO[0] = {}
    if os.environ.get("GITHUB_ACTIONS") != "true" or not chave_reservas(): return SESSAO[0]
    try:
        git("fetch", "-q", "--depth", "1", remoto(), "dados")
        bruto = base64.b64decode(git("show", "FETCH_HEAD:sessao.bin").stdout.strip())
        garantir_cripto()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        SESSAO[0] = json.loads(AESGCM(chave_reservas()).decrypt(bruto[:12], bruto[12:], None))
    except Exception:
        pass
    return SESSAO[0]

def salvar_sessao():
    if os.environ.get("GITHUB_ACTIONS") != "true" or not chave_reservas() or not garantir_cripto(): return
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        open("sessao.bin", "w").write(base64.b64encode(nonce + AESGCM(chave_reservas()).encrypt(nonce, json.dumps(SESSAO[0]).encode(), None)).decode())
        antes = SO_ARQUIVOS[0]; SO_ARQUIVOS[0] = ("sessao.bin",)
        try: salvar_dados()
        finally: SO_ARQUIVOS[0] = antes
    except Exception as e:
        print("  AVISO: não consegui guardar a sessão:", e)

RETRY = [0.0]
def pausar(s):
    """Barrado (429): espera o prazo que o forinvest pediu (Retry-After) ou 20 min, dobrando a cada bloqueio seguido (até 3 h)."""
    n = int(s.get("bloqueios", 0)) + 1; s["bloqueios"] = n
    s["pausa"] = time.time() + max(RETRY[0] + 60, min(180, 20 * 2 ** (n - 1)) * 60)

def login(qual, email, senha, espera=60):
    """Token do forinvest: reaproveita o guardado enquanto vale (folga de 15 min); senão faz login (1 vez)."""
    s = ler_sessao(); tok = s.get(qual)
    if tok and max(jwt_exp(tok), s.get(qual + "_ate", 0)) - time.time() > 900: return tok
    try:
        tok = achar_token(chamar(BASE + "Users/authenticate", {"email": email, "password": senha, "rememberMe": ""}, espera=espera))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            ra = e.headers.get("Retry-After") or ""
            if ra.replace(".", "").isdigit(): RETRY[0] = float(ra)
            print("  Resposta do forinvest:", (e.headers.get("Retry-After") or "sem prazo"), "|", (e.read() or b"")[:300].decode("utf-8", "replace"))
            pausar(s); salvar_sessao()
            raise Pausa(qual)
        raise
    if tok:
        s[qual] = tok
        if not jwt_exp(tok): s[qual + "_ate"] = time.time() + 3600   # token sem validade escrita: reusa por 1 hora
        salvar_sessao()
    return tok

def ADMIN_OK():
    return bool(os.environ.get("FORINVEST_ADMIN_EMAIL") and os.environ.get("FORINVEST_ADMIN_SENHA"))

def token_admin(token):
    if ADMIN_OK() and TOKEN[0] == token and not os.environ.get("FORINVEST_EMAIL"): return token
    if not (os.environ.get("FORINVEST_ADMIN_EMAIL") and os.environ.get("FORINVEST_ADMIN_SENHA")):
        print("  AVISO: secrets FORINVEST_ADMIN_EMAIL / FORINVEST_ADMIN_SENHA não encontrados: o log precisa do acesso administrativo.")
    if os.environ.get("FORINVEST_ADMIN_EMAIL") and os.environ.get("FORINVEST_ADMIN_SENHA"):
        try:
            return login("admin", os.environ["FORINVEST_ADMIN_EMAIL"], os.environ["FORINVEST_ADMIN_SENHA"], espera=20) or token
        except Exception as e:
            print("  AVISO: login administrativo falhou:", e)
    return token

def ler_log(tok, ate_id=None, paginas=6, por=50):
    """Registros do log de integração, do mais novo para o mais velho, até achar ate_id. Retorna (registros, achou)."""
    regs = []
    for pg in range(paginas):
        r = chamar(BASE + f"OData/RmIntegrationLog?count=true&top={por}&skip={pg * por}&orderby=createdDate%20desc,id%20asc", token=tok, espera=15)
        lista = r.get("value") if isinstance(r, dict) else r
        if not lista: return regs, ate_id is None
        for x in lista:
            if ate_id and x.get("id") == ate_id: return regs, True
            regs.append(x)
        if not ate_id: return regs, True
    return regs, False

def rodada_pelo_log(token):
    """Lê só os registros NOVOS do log do forinvest e troca só os lotes citados. Em segundos.
    Retorna False quando é preciso a conferência completa."""
    try:
        tok = token_admin(token)
        ant = status_anterior()
        try: esperados = len(json.loads(re.sub(r",\s*([\]}])", r"\1", open("loteamentos.json", encoding="utf-8").read())))
        except Exception: esperados = 0
        if esperados and len(ant.get("s") or {}) < esperados and time.time() - float(ant.get("cheio") or 0) > 25 * 60:     # falta loteamento nos dados: baixa tudo de novo
            print(f"Faltam loteamentos nos dados ({len(ant.get('s') or {})} de {esperados}): conferência completa."); return False
        if not ant.get("log_id") or time.time() - float(ant.get("cheio") or 0) > 180 * 60:
            print("Conferência completa (primeira vez com o log ou passaram 3 horas)."); return False
        novos, achou = ler_log(tok, ant["log_id"])
        if not achou: print("Muitos registros novos no log: conferência completa."); return False
        vencidas = [(lid, int(i)) for lid, m in (ant.get("exp") or {}).items() for i, ate in m.items() if ate and ate < agora_br().isoformat()]
        if not novos and not vencidas:
            # nada mudou: só registra a hora da conferência (o site mostra "Atualizado às …")
            ant["conferido"] = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M")
            json.dump(ant, open("status.json", "w", encoding="utf-8"), separators=(",", ":"))
            if os.environ.get("GITHUB_ACTIONS") == "true":
                arquivos_so = ("status.json",); SO_ARQUIVOS[0] = arquivos_so; salvar_dados(); SO_ARQUIVOS[0] = None
            print("Nada mudou no forinvest (log de integração sem registros novos). Pronto."); return True
        dados, unids = ler_do_ramo("data.json"), ler_do_ramo("unidades.json")
        if not dados or not unids: return False
        reservas = abrir_reservas(ler_do_ramo("reservas.json") or {}) if chave_reservas() else {}
        if reservas is None: return False        # não abriu: confere tudo em vez de apagar as reservas antigas
        por_id = {L["id"]: L for L in dados["loteamentos"]}
        ofertas = {it["offerId"]: it for it in json.loads(re.sub(r",\s*([\]}])", r"\1", open("loteamentos.json", encoding="utf-8").read()))}
        baixar, mudou = set(), 0
        for lid, i in vencidas:                                        # reservas de 48h que já venceram
            L = por_id.get(lid)
            if L and i < len(L["lotes"]) and L["lotes"][i][3] == 2:
                L["lotes"][i][3] = 0; mudou += 1
                (reservas.get(lid) or {}).pop(f"{L['lotes'][i][0]}|{L['lotes'][i][1]}", None)
                print(f"  {L['nome']}: lote {L['lotes'][i][1]} -> disponível (reserva de 48h venceu)")
        for x in reversed(novos):                                       # do mais velho para o mais novo
            if not x.get("success"): continue
            alvo = unids.get(x.get("unitId") or "")
            cod = (re.search(r"=(\d+)", x.get("rmCode") or "") or [None, None])[1]
            if x.get("operationDescription") != "Atualização de status da unidade" or cod not in ST_RM or not alvo or alvo[0] not in por_id:
                if x.get("offerId") in ofertas: baixar.add(x["offerId"])   # venda, reserva definitiva etc.: confere só esse loteamento
                continue
            lid, chave = alvo
            lote = next((l for l in por_id[lid]["lotes"] if f"{l[0]}|{l[1]}" == chave), None)
            if not lote: baixar.add(x.get("offerId")); continue
            novo = ST_RM[cod]
            if lote[3] != novo: lote[3] = novo; mudou += 1
            if novo == 2:
                desde = (x.get("createdDate") or "")[:19]
                try: ate = (datetime.fromisoformat(desde) + timedelta(hours=48)).isoformat()
                except Exception: ate = None
                reservas.setdefault(lid, {})[chave] = {"nome": nome_pelo_email(x.get("createdByEmail")), "email": x.get("createdByEmail"), "desde": desde or None, "ate": ate}
            else:
                (reservas.get(lid) or {}).pop(chave, None)
            print(f"  {por_id[lid]['nome']}: lote {chave.replace('|', ' / ')} -> {'reservado por ' + (nome_pelo_email(x.get('createdByEmail')) or '?') if novo == 2 else 'disponível'}")
        baixar.discard(None)
        if baixar:
            print("  conferindo por completo só:", ", ".join(ofertas[o]["id"] for o in baixar if o in ofertas))
            for item, offer, erro in baixar_todos([ofertas[o] for o in baixar if o in ofertas], token):
                if erro: print("ERRO em", item["id"], erro); continue
                RESERVAS_TMP.pop(item["id"], None)
                L = montar(offer, item)
                antigo = por_id.get(item["id"])
                if antigo and antigo.get("planta"): L["planta"] = antigo["planta"]
                velhos = reservas.get(item["id"], {})
                novas = RESERVAS_TMP.get(item["id"], {})
                for k, v in novas.items():
                    if not v.get("nome") and velhos.get(k, {}).get("nome"): v["nome"] = velhos[k]["nome"]   # mantém o corretor
                reservas[item["id"]] = novas
                if antigo: dados["loteamentos"][dados["loteamentos"].index(antigo)] = L
                else: dados["loteamentos"].append(L)
                for uid, alvo in list(UNIDS_TMP.items()): unids[uid] = alvo
                mudou += 1
        agora = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M")
        dados["atualizado"] = agora
        json.dump(dados, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        json.dump(unids, open("unidades.json", "w", encoding="utf-8"), separators=(",", ":"))
        json.dump({"atualizado": agora, "log_id": novos[0]["id"] if novos else ant.get("log_id"), "cheio": ant.get("cheio"),
                   "exp": vencimentos(por_id, reservas),
                   "s": {L["id"]: "".join(str(min(9, max(0, int(l[3] or 0)))) for l in L["lotes"]) for L in dados["loteamentos"]}},
                  open("status.json", "w", encoding="utf-8"), separators=(",", ":"))
        RESERVAS_TMP.clear(); RESERVAS_TMP.update(reservas)
        salvar_reservas()
        if os.environ.get("GITHUB_ACTIONS") == "true": salvar_dados()
        print(f"Pelo log: {len(novos)} registros novos, {len(vencidas)} reservas vencidas, {mudou} mudanças aplicadas.")
        return True
    except Exception as e:
        print("  AVISO: não deu para usar o log (", e, "): conferência completa.")
        return False

def anterior_dados():
    """data.json da rodada anterior (ramo "dados"), para reaproveitar as plantas já cortadas."""
    try:
        git("fetch", "-q", "--depth", "1", remoto(), "dados")
        return {L["id"]: L for L in json.loads(git("show", "FETCH_HEAD:data.json").stdout).get("loteamentos", [])}
    except Exception:
        return {}

def disparar(workflow):
    """Pede ao GitHub para rodar outra rotina (ex.: cortar a planta de um loteamento novo)."""
    tok, repo = os.environ.get("GH_TOKEN"), REPO
    if not tok or not repo: return
    r = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
                               data=json.dumps({"ref": "main"}).encode(), method="POST",
                               headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"})
    try: urllib.request.urlopen(r, timeout=30); print("Pedi para cortar as plantas novas (rotina Atualizar plantas).")
    except Exception as e: print("  AVISO: não consegui chamar a rotina das plantas:", e)

def main():
    rapido = len(sys.argv) > 1 and sys.argv[1] == "rapido"
    # de madrugada (meia-noite às 6h, horário de Brasília) não consulta o forinvest: termina na hora.
    # Reservas que vencem nesse horário o próprio site libera sozinho; às 6h o robô pega tudo o que mudou.
    hora = datetime.now(timezone(timedelta(hours=-3))).hour
    if rapido and hora < 6 and os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch_forcado":
        print(f"Madrugada ({hora}h): sem consulta ao forinvest. Volta às 6h."); return
    texto = open("loteamentos.json", encoding="utf-8").read()
    lista = json.loads(re.sub(r",\s*([\]}])", r"\1", texto))  # ignora vírgula sobrando no fim
    if os.environ.get("GITHUB_ACTIONS") == "true":
        git("config", "user.name", "robo-lotes"); git("config", "user.email", "robo-lotes@users.noreply.github.com")
        print("Lotes e plantas vão para:", DADOS_REPO, "(remote " + remoto() + ")")
    print("Caminho até o forinvest:", "ponte do Google ✓" if os.environ.get("PONTE_URL") else "direto do GitHub (ponte NÃO configurada: falta o Secret PONTE_URL no workflow)")
    conta = (os.environ.get("FORINVEST_EMAIL") or "") + "|" + (os.environ.get("FORINVEST_ADMIN_EMAIL") or "")
    if ler_sessao().get("conta") != conta:          # trocou a conta nos Secrets: começa do zero (sem token nem espera da conta antiga)
        SESSAO[0] = {"conta": conta}
    pausa = ler_sessao().get("pausa", 0)
    if pausa > time.time():
        print(f"O forinvest pediu para esperar (limite de acessos). Tento de novo em {int((pausa - time.time()) / 60) + 1} min."); return
    try:
        # lotes: login de CORRETOR (só ele recebe a localização de cada lote; o de administrador vem sem coordenadas)
        if os.environ.get("FORINVEST_EMAIL") and os.environ.get("FORINVEST_SENHA"):
            token = login("corretor", os.environ["FORINVEST_EMAIL"], os.environ["FORINVEST_SENHA"])
        else:
            token = login("admin", os.environ["FORINVEST_ADMIN_EMAIL"], os.environ["FORINVEST_ADMIN_SENHA"])
    except Pausa:
        print(f"O forinvest barrou o login (429). Espero {int((ler_sessao()['pausa'] - time.time()) / 60)} min e tento sozinho de novo."); return
    if not token: sys.exit("Login falhou: a resposta não trouxe token. Confira e-mail e senha nos Secrets.")
    TOKEN[0] = token
    if rapido and rodada_pelo_log(token): return
    try:   # marca o registro mais novo do log agora, antes de conferir tudo (o que chegar depois entra na próxima rodada)
        topo, _ = ler_log(token_admin(token), None, 1, 1); MARCA[0] = topo[0]["id"] if topo else None
    except Exception as e: print("  AVISO: log do forinvest indisponível:", e)
    reservas_velhas = (abrir_reservas(ler_do_ramo("reservas.json") or {}) if (status_anterior() is not None and chave_reservas()) else {}) or {}
    antes = anterior_dados() if rapido else {}
    if not rapido: preparar_plantas()
    falta_planta = False
    saida = []
    nomes = {0: "disponíveis", 1: "vendidos", 2: "reservados", 3: "indisponíveis", 4: "reservados definitivo"}
    ultimos[0] = baixar_todos(lista, token)
    for item, offer, erro in ultimos[0]:
        if erro:
            print("ERRO em", item.get("id"), erro)
            if antes.get(item.get("id")): saida.append(antes[item["id"]])   # mantém os dados anteriores desse loteamento
            continue
        try:
            lot = montar(offer, item)
            if rapido:
                # reaproveita a planta já cortada da rodada anterior (só refaz se a planta mudou no forinvest)
                ant = (antes.get(lot["id"]) or {}).get("planta")
                src = (lot.get("planta") or {}).get("url")
                if src and ant and src in (ant.get("original"), ant.get("url")): lot["planta"] = ant
                elif src: falta_planta = True
            else:
                otimizar_planta(lot)
            saida.append(lot)
            cont = {}
            for l in lot["lotes"]: cont[l[3]] = cont.get(l[3], 0) + 1
            print("OK", lot["nome"], len(lot["lotes"]), "lotes:", ", ".join(f"{n} {nomes.get(k, 'código ' + str(k))}" for k, n in sorted(cont.items(), key=lambda x: str(x[0]))))
        except Exception as e:
            print("ERRO em", item.get("id"), e)
    if not any(o for _, o, _ in ultimos[0]):
        saida = []
    if saida and antes and len(saida) < len(antes) and len(saida) < len(lista):
        ids = {L["id"] for L in saida}
        for item in lista:                                   # garante: nenhum loteamento que já estava no site some
            if item.get("id") not in ids and antes.get(item.get("id")): saida.append(antes[item["id"]])
        print(f"  Mantidos os dados anteriores de {len(lista) - len(ids)} loteamento(s) que não baixaram agora.")
    faltando = len({L["id"] for L in saida}) < len(lista)
    if not saida:
        if any(isinstance(er, Pausa) or getattr(er, "code", 0) == 429 for _, _, er in ultimos[0]):
            pausar(ler_sessao()); salvar_sessao()
            print(f"O forinvest está limitando os pedidos (429). Espero {int((ler_sessao()['pausa'] - time.time()) / 60)} min e tento sozinho de novo."); return
        sys.exit("Nenhum loteamento baixado.")
    s = ler_sessao()
    if s.get("bloqueios") or s.get("pausa"):           # voltou a funcionar: zera a contagem de bloqueios
        s.pop("bloqueios", None); s.pop("pausa", None); salvar_sessao()
    agora = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M")
    json.dump({"atualizado": agora, "loteamentos": saida}, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    # arquivinho só com a situação de cada lote (0 a 4), na mesma ordem: o site confere ele a cada minuto e troca as cores na hora
    # quem reservou: pega no log do forinvest a reserva mais recente de cada lote reservado
    try:
        r = chamar(BASE + "OData/RmIntegrationLog?count=true&top=400&skip=0&orderby=createdDate%20desc,id%20asc", token=token_admin(token), espera=30)
        pelo_log = {}
        for x in (r.get("value") or []):
            if x.get("success") and "=903" in (x.get("rmCode") or "") and x.get("unitId") not in pelo_log: pelo_log[x["unitId"]] = x
        achados = 0
        for uid, (lid, k) in UNIDS_TMP.items():
            x = pelo_log.get(uid); v = (RESERVAS_TMP.get(lid) or {}).get(k)
            if x and v is not None and x.get("createdByEmail"):
                v["email"] = x["createdByEmail"]; v["nome"] = v.get("nome") or nome_pelo_email(x["createdByEmail"]); achados += 1
        print("  corretor das reservas achado no log:", achados)
    except Exception as e: print("  AVISO: não consegui ler o log para os nomes:", e)
    for lid, rs in RESERVAS_TMP.items():          # mantém o nome do corretor que veio do log
        for k, v in rs.items():
            velho = (reservas_velhas.get(lid) or {}).get(k) or {}
            if not v.get("nome") and velho.get("nome"): v["nome"] = velho["nome"]
            if not v.get("email") and velho.get("email"): v["email"] = velho["email"]
    json.dump(UNIDS_TMP, open("unidades.json", "w", encoding="utf-8"), separators=(",", ":"))
    json.dump({"atualizado": agora, "log_id": MARCA[0], "cheio": (time.time() - 150 * 60) if faltando else time.time(), "exp": vencimentos({L["id"]: L for L in saida}, RESERVAS_TMP), "s": {L["id"]: "".join(str(min(9, max(0, int(l[3] or 0)))) for l in L["lotes"]) for L in saida}},
              open("status.json", "w", encoding="utf-8"), separators=(",", ":"))
    if rapido:
        salvar_reservas()
        if os.environ.get("GITHUB_ACTIONS") == "true": salvar_dados()
        if falta_planta: disparar("atualizar-plantas.yml")
        return
    open(".nojekyll", "a").close()   # o GitHub Pages publica os milhares de pedaços de planta sem processar
    pastas = []
    if os.path.exists("index.html"):
        pagina = CASCA
        for item in lista:
            pasta = re.sub(r"[^a-z0-9-]", "", item["id"].lower())
            if not pasta: continue
            os.makedirs(pasta, exist_ok=True); pastas.append(pasta)
            destino = os.path.join(pasta, "index.html")
            if not os.path.exists(destino) or open(destino, encoding="utf-8").read() != pagina:
                open(destino, "w", encoding="utf-8").write(pagina)
    publicar_plantas()
    salvar_reservas()
    salvar_no_github(pastas)

def git(*args, ok=False):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode and not ok: raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[-400:]}")
    return r

SO_ARQUIVOS = [None]

def salvar_dados():
    """Os lotes (data.json) e as reservas ficam no ramo "dados". O site lê de lá na hora, e o GitHub Pages
    não precisa republicar o site inteiro a cada 5 minutos. Grava direto no ramo, sem baixar o site (rápido)."""
    arquivos = {n: open(n, "rb").read() for n in (SO_ARQUIVOS[0] or ("data.json", "reservas.json", "status.json", "unidades.json")) if os.path.exists(n)}
    for tentativa in range(1, 6):
        tem = git("fetch", "-q", "--depth", "1", remoto(), "dados", ok=True).returncode == 0
        pai = git("rev-parse", "FETCH_HEAD").stdout.strip() if tem else None
        entradas = {}
        if pai:
            for linha in git("ls-tree", pai).stdout.splitlines():
                info, nome = linha.split("\t", 1); m, t, h = info.split(); entradas[nome] = (m, t, h)
        mudou = False
        for nome, dados in arquivos.items():
            h = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=dados, capture_output=True, check=True).stdout.decode().strip()
            if entradas.get(nome, (None, None, None))[2] != h: mudou = True
            entradas[nome] = ("100644", "blob", h)
        if not mudou: print("Lotes sem mudança no ramo dados."); return
        arvore = subprocess.run(["git", "mktree"], input="".join(f"{m} {t} {h}\t{n}\n" for n, (m, t, h) in sorted(entradas.items())).encode(),
                                capture_output=True, check=True).stdout.decode().strip()
        commit = git("commit-tree", arvore, "-m", "Atualiza lotes").stdout.strip()            # sem histórico: o ramo não cresce
        if git("push", "-q", "--force", remoto(), f"{commit}:refs/heads/dados", ok=True).returncode == 0:
            print("Lotes salvos no ramo dados."); return
        print(f"Tentando salvar os lotes de novo ({tentativa}/5)..."); time.sleep(2 * tentativa)
    sys.exit("Não consegui salvar os lotes no ramo dados.")

def salvar_no_github(pastas):
    """Salva só os arquivos do robô, por cima da versão mais nova do GitHub.
    Não usa "merge": nunca dá conflito e nunca desfaz o que o painel do corretor salvou."""
    if os.environ.get("GITHUB_ACTIONS") != "true": return
    git("config", "user.name", "robo-lotes"); git("config", "user.email", "robo-lotes@users.noreply.github.com")
    salvar_dados()
    shutil.rmtree("plantas", ignore_errors=True)        # as plantas agora ficam no ramo "plantas", fora do site
    meus = [".nojekyll"] + [p for p in pastas if os.path.isdir(p)]
    for tentativa in range(1, 6):
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--mixed", "origin/main")          # parte da versão mais nova, mantendo os arquivos gerados agora
        git("add", "-A", "--", *[m for m in meus if os.path.exists(m)])
        git("rm", "-r", "-q", "--cached", "--ignore-unmatch", "plantas")
        git("reset", "-q", "--", "data.json", "reservas.json", ok=True)   # esses dois vão só para o ramo "dados"
        if git("diff", "--cached", "--quiet", ok=True).returncode == 0:
            print("Nada mudou para salvar."); git("reset", "-q", "--hard", "origin/main"); return
        git("commit", "-q", "-m", "Atualiza lotes")
        if git("push", "-q", "origin", "HEAD:main", ok=True).returncode == 0:
            print("Salvo no GitHub."); git("reset", "-q", "--hard", "HEAD"); return
        print(f"Outra alteração entrou no meio, tentando de novo ({tentativa}/5)...")
        time.sleep(5 * tentativa)
    sys.exit("Não consegui salvar no GitHub depois de 5 tentativas.")

if __name__ == "__main__":
    try:
        main()
    finally:
        if RECUSADO[0] and SESSAO[0]:
            SESSAO[0].pop("corretor", None); SESSAO[0].pop("admin", None); salvar_sessao()
