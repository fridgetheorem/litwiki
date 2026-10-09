"""Paper map: a 2-D layout of every paper, written as a static page (clusters/index.html) into the site build.

Two layouts, switchable in the page:
  tldr     TF-IDF over the chosen page sections (default: the TL;DR) -> SVD -> t-SNE
  concept  concept membership (binary) -> SVD -> t-SNE
Points are coloured by each paper's primary (first-listed) concept. Layouts are cached in state/ and only
recomputed when a paper's features change; new layouts are rotated/scaled (Procrustes) onto the previous
one so the map doesn't flip around every time a paper is added."""
import hashlib
import json
import re
from pathlib import Path

import numpy as np

from . import vault
from .common import STATE_DIR, log, read_page

CACHE = STATE_DIR / "clusters.json"
SECTION = re.compile(r"^## (.+?)\s*$", re.M)


def section_text(body: str, names: list[str]) -> str:
    """Text of the named '## Heading' sections of a paper page, concatenated."""
    marks = [(m.group(1), m.start(), m.end()) for m in SECTION.finditer(body)]
    out = []
    for i, (name, _, end) in enumerate(marks):
        if name in names:
            out.append(body[end:marks[i + 1][1] if i + 1 < len(marks) else len(body)])
    text = " ".join(out)
    text = re.sub(r"\[\[[^\]|]*\|?([^\]]*)\]\]", r"\1", text)      # wikilinks -> label
    text = re.sub(r"<!--.*?-->|[>*_`#]|\[[^\]]*\]\([^)]*\)", " ", text, flags=re.S)
    return " ".join(text.split())


def load_papers(cfg) -> list[dict]:
    names = (cfg.get("clusters") or {}).get("sections") or ["TL;DR"]
    papers = []
    for p in sorted((cfg["vault"] / vault.PAPERS).glob("*.md")):
        fm, body = read_page(p)
        if not fm.get("title"):
            continue
        cs = [c for c in (fm.get("concepts") or []) if isinstance(c, str)]
        papers.append({
            "id": p.stem, "title": str(fm["title"]), "year": fm.get("year"), "concepts": cs,
            "authors": ", ".join(map(str, (fm.get("authors") or [])[:3])) + (" et al." if len(fm.get("authors") or []) > 3 else ""),
            "text": section_text(body, names),
        })
    return papers


def _layout(X, seed: int = 0) -> np.ndarray:
    """Dense/sparse feature matrix -> (n, 2) t-SNE coordinates."""
    from sklearn.decomposition import TruncatedSVD
    from sklearn.manifold import TSNE
    n = X.shape[0]
    k = max(2, min(50, n - 1, X.shape[1] - 1))
    Z = TruncatedSVD(n_components=k, random_state=seed).fit_transform(X)
    perplexity = max(2.0, min(30.0, (n - 1) / 3))
    Y = TSNE(n_components=2, perplexity=perplexity, init="pca", random_state=seed,
             learning_rate="auto", metric="euclidean").fit_transform(Z)
    # Identical feature vectors (e.g. same concept set) would sit exactly on top of each other.
    Y += np.random.default_rng(seed).normal(scale=0.02 * (Y.std() or 1), size=Y.shape)
    return Y


def _normalise(Y: np.ndarray) -> np.ndarray:
    Y = Y - Y.mean(axis=0)
    return Y / (np.abs(Y).max() or 1)


def _align(Y: np.ndarray, ids: list[str], prev: dict) -> np.ndarray:
    """Rotate/reflect/scale Y onto the previous layout of the papers both have (keeps the map stable)."""
    from scipy.linalg import orthogonal_procrustes
    common = [i for i, pid in enumerate(ids) if pid in prev]
    if len(common) < 10:
        return Y
    A = Y[common]
    B = np.array([prev[ids[i]] for i in common])
    ca, cb = A.mean(axis=0), B.mean(axis=0)
    R, _ = orthogonal_procrustes(A - ca, B - cb)
    scale = np.linalg.norm(B - cb) / (np.linalg.norm(A - ca) or 1)
    return (Y - ca) @ R * scale + cb


def compute_layouts(cfg, papers: list[dict]) -> dict[str, dict[str, list[float]]]:
    """{'tldr': {id: [x, y]}, 'concept': {...}}; cached while features are unchanged."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    key = hashlib.sha1(json.dumps([cfg.get("clusters"), [(p["id"], p["text"], p["concepts"]) for p in papers]],
                                  sort_keys=True).encode()).hexdigest()
    prev = {}
    if CACHE.exists():
        cached = json.loads(CACHE.read_text())
        if cached.get("key") == key:
            return cached["layouts"]
        prev = cached.get("layouts", {})
    ids = [p["id"] for p in papers]
    min_df = int((cfg.get("clusters") or {}).get("min_df", 2))

    for df in (min_df, 1):  # tiny libraries may have no term shared by two papers
        try:
            tfidf = TfidfVectorizer(stop_words="english", sublinear_tf=True, ngram_range=(1, 2),
                                    min_df=df, max_df=0.5).fit_transform([p["text"] for p in papers])
            if tfidf.shape[1] > 2:
                break
        except ValueError:
            continue
    slugs = sorted({c for p in papers for c in p["concepts"]})
    member = np.zeros((len(papers), max(len(slugs), 1)))
    for i, p in enumerate(papers):
        for c in p["concepts"]:
            member[i, slugs.index(c)] = 1.0
    # Down-weight very common concepts (IDF) so the layout isn't dominated by one big topic.
    df_c = member.sum(axis=0)
    member = member * np.log((1 + len(papers)) / (1 + df_c))

    layouts = {}
    for name, X in (("tldr", tfidf), ("concept", member)):
        Y = _normalise(_layout(X))
        Y = _normalise(_align(Y, ids, prev.get(name, {})))
        layouts[name] = {pid: [round(float(x), 4), round(float(y), 4)] for pid, (x, y) in zip(ids, Y)}
    STATE_DIR.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({"key": key, "layouts": layouts}))
    return layouts


PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
           "#9c755f", "#17becf", "#8c6bb1", "#d4a017"]


def build_data(cfg) -> dict | None:
    papers = load_papers(cfg)
    if len(papers) < 5:
        return None
    layouts = compute_layouts(cfg, papers)
    titles = {s: fm["title"] for s, fm in vault.concepts(cfg["vault"]).items()}
    counts: dict[str, int] = {}
    for p in papers:
        if p["concepts"]:
            counts[p["concepts"][0]] = counts.get(p["concepts"][0], 0) + 1
    top = [c for c, _ in sorted(counts.items(), key=lambda x: -x[1])[:len(PALETTE)]]
    return {
        "papers": [{
            "id": p["id"], "t": p["title"], "a": p["authors"], "y": p["year"], "c": p["concepts"],
            "p": p["concepts"][0] if p["concepts"] else "", "s": p["text"][:420],
            "xy": {name: layouts[name][p["id"]] for name in layouts},
        } for p in papers],
        "concepts": {s: titles.get(s, s) for s in sorted({c for p in papers for c in p["concepts"]})},
        "top": top, "colors": dict(zip(top, PALETTE)),
        "layouts": {"tldr": "TL;DR text similarity", "concept": "Concept membership"},
    }


def write_page(cfg, out: Path) -> bool:
    data = build_data(cfg)
    if data is None:
        return False
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = out / "clusters" / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(PAGE.replace("__DATA__", blob), encoding="utf-8")
    log.info("cluster map: %d papers", len(data["papers"]))
    return True


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Paper map · Literature Wiki</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1d2330;--mut:#667085;--line:#d9dde5;--acc:#3f51b5;--card:#f6f7fb;--other:#b4bac6}
@media(prefers-color-scheme:dark){:root{--bg:#14161c;--fg:#e4e7ee;--mut:#98a2b3;--line:#2c313c;--acc:#8c9eff;--card:#1c1f27;--other:#5b6372}}
*{box-sizing:border-box}html,body{height:100%;margin:0}body{display:flex;flex-direction:column;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
header{display:flex;gap:.8rem;flex-wrap:wrap;align-items:center;padding:.5rem .8rem;border-bottom:1px solid var(--line);background:var(--card)}
header a{color:var(--acc);text-decoration:none}header .sp{flex:1}
select,input{padding:.3rem .5rem;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit;max-width:16rem}
#wrap{flex:1;display:flex;min-height:0}#plot{flex:1;min-width:0;cursor:grab;touch-action:none}#plot.drag{cursor:grabbing}
#legend{width:15rem;overflow:auto;padding:.6rem;border-left:1px solid var(--line);font-size:.85rem}
#legend div{display:flex;gap:.5rem;align-items:center;padding:.15rem 0;cursor:pointer}#legend div.off{opacity:.35}
#legend i{width:.8rem;height:.8rem;border-radius:50%;flex:none}#legend small{color:var(--mut);margin-left:auto}
circle{stroke:var(--bg);stroke-width:1;vector-effect:non-scaling-stroke}circle.dim{opacity:.08;pointer-events:none}
#tip{position:fixed;pointer-events:none;max-width:24rem;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.5rem .7rem;font-size:.85rem;box-shadow:0 4px 14px #0004;display:none}
#tip b{display:block}#tip .m{color:var(--mut)}@media(max-width:700px){#legend{display:none}}
</style></head><body>
<header><a href="/"><b>← Wiki</b></a><a href="/app/">Manage</a><span class="sp"></span>
<label>Layout <select id="layout"></select></label>
<label>Concept <select id="concept"><option value="">all</option></select></label>
<label>From <select id="year"></select></label>
<input id="q" type="search" placeholder="Search title, authors, TL;DR"><span id="count" style="color:var(--mut)"></span></header>
<div id="wrap"><svg id="plot"><g id="world"></g></svg><div id="legend"></div></div><div id="tip"></div>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id),NS="http://www.w3.org/2000/svg";
const svg=$("plot"),world=$("world"),tip=$("tip");
let layout="tldr",view={x:0,y:0,k:1},hiddenTop=new Set(),dots=[];
for(const [k,v] of Object.entries(D.layouts))$("layout").add(new Option(v,k));
for(const [s,t] of Object.entries(D.concepts).sort((a,b)=>a[1].localeCompare(b[1])))$("concept").add(new Option(t,s));
const years=[...new Set(D.papers.map(p=>p.y).filter(Boolean))].sort();
$("year").add(new Option("any",""));for(const y of years)$("year").add(new Option(y,y));
const colour=p=>D.colors[p.p]||"var(--other)";
const size=()=>{const r=svg.getBoundingClientRect();return{w:r.width,h:r.height,s:Math.min(r.width,r.height)*0.46}};
const pos=(p,l=layout)=>{const{w,h,s}=size();return[w/2+p.xy[l][0]*s,h/2+p.xy[l][1]*s]};
function build(){
  world.replaceChildren();dots=[];
  for(const p of D.papers){
    const a=document.createElementNS(NS,"a");a.setAttribute("href","/papers/"+encodeURIComponent(p.id)+"/");
    const c=document.createElementNS(NS,"circle");c.setAttribute("fill",colour(p));
    a.append(c);world.append(a);dots.push({p,c,a});
    a.addEventListener("mousemove",e=>show(e,p));a.addEventListener("mouseleave",()=>tip.style.display="none");
  }
  place();filter();
}
function place(){const r=5/view.k;for(const d of dots){const[x,y]=pos(d.p);d.c.setAttribute("cx",x);d.c.setAttribute("cy",y);d.c.setAttribute("r",r)}
  world.setAttribute("transform",`translate(${view.x} ${view.y}) scale(${view.k})`)}
function show(e,p){const cs=p.c.map(c=>D.concepts[c]).join(" · ");
  tip.innerHTML="<b></b><div class=m></div><div style='margin-top:.3rem'></div><div class=m style='margin-top:.3rem'></div>";
  const[t,m,s,c]=tip.children;t.textContent=p.t;m.textContent=[p.a,p.y].filter(Boolean).join(", ");s.textContent=p.s+(p.s.length>=420?"…":"");c.textContent=cs;
  tip.style.display="block";const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8),y=Math.min(e.clientY+14,innerHeight-tip.offsetHeight-8);
  tip.style.left=x+"px";tip.style.top=y+"px"}
function filter(){
  const q=$("q").value.toLowerCase().trim().split(/\s+/).filter(Boolean),con=$("concept").value,y=+$("year").value||0;let n=0;
  for(const d of dots){const p=d.p;
    const ok=(!con||p.c.includes(con))&&(!y||(p.y&&p.y>=y))&&!hiddenTop.has(D.colors[p.p]?p.p:"")
      &&q.every(w=>(p.t+" "+p.a+" "+p.s).toLowerCase().includes(w));
    d.c.classList.toggle("dim",!ok);n+=ok;if(ok)world.append(d.a)} // matches drawn on top
  $("count").textContent=n+" / "+dots.length}
function legend(){const el=$("legend");el.replaceChildren();const cnt={};for(const p of D.papers)cnt[p.p]=(cnt[p.p]||0)+1;
  for(const s of [...D.top,""]){const row=document.createElement("div");const i=document.createElement("i");i.style.background=D.colors[s]||"var(--other)";
    const l=document.createElement("span");l.textContent=s?D.concepts[s]:"Other / no concept";const n=document.createElement("small");
    n.textContent=s?cnt[s]:D.papers.filter(p=>!D.colors[p.p]).length;row.append(i,l,n);
    row.title="Click to hide/show";row.onclick=()=>{hiddenTop.has(s)?hiddenTop.delete(s):hiddenTop.add(s);row.classList.toggle("off");filter()};el.append(row)}}
$("layout").onchange=e=>{const from=D.papers.map(p=>pos(p)),to=e.target.value;const t0=performance.now();
  const step=t=>{const f=Math.min(1,(t-t0)/600),k=f*f*(3-2*f);
    dots.forEach((d,i)=>{const[x,y]=pos(d.p,to);d.c.setAttribute("cx",from[i][0]+(x-from[i][0])*k);d.c.setAttribute("cy",from[i][1]+(y-from[i][1])*k)});
    if(f<1)requestAnimationFrame(step);else{layout=to;place()}};requestAnimationFrame(step)};
for(const id of["concept","year","q"])$(id).addEventListener("input",filter);
// pan / zoom
let drag=null;svg.addEventListener("pointerdown",e=>{drag={x:e.clientX,y:e.clientY,vx:view.x,vy:view.y,moved:false}});
addEventListener("pointermove",e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>3){drag.moved=true;svg.classList.add("drag")}
  if(drag.moved){view.x=drag.vx+dx;view.y=drag.vy+dy;place()}});
addEventListener("pointerup",()=>{svg.classList.remove("drag");setTimeout(()=>drag=null)});
svg.addEventListener("click",e=>{if(drag&&drag.moved)e.preventDefault()},true);
svg.addEventListener("wheel",e=>{e.preventDefault();const r=svg.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  const k=Math.max(.5,Math.min(40,view.k*Math.exp(-e.deltaY*0.0015)));view.x=mx-(mx-view.x)*k/view.k;view.y=my-(my-view.y)*k/view.k;view.k=k;place()},{passive:false});
addEventListener("resize",place);legend();build();
</script></body></html>
"""
