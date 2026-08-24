# ir-watch

Monitor automatizado das divulgações financeiras e operacionais periódicas de
uma watch list de 12 empresas do setor fitness.

O sistema faz **uma coisa**: detecta que uma empresa monitorada publicou uma
nova divulgação periódica relevante e envia **um e-mail por evento lógico**,
sem duplicatas.

Ele **não** monitora mudanças genéricas nas páginas de RI, não resume releases,
não extrai métricas financeiras, não monitora preço de ação e não depende de
LLM em runtime.

---

## Índice

- [Empresas monitoradas](#empresas-monitoradas)
- [O que gera e o que não gera alerta](#o-que-gera-e-o-que-não-gera-alerta)
- [Arquitetura](#arquitetura)
- [Instalação local](#instalação-local)
- [Environment variables](#environment-variables)
- [Bootstrap](#bootstrap)
- [Dry run](#dry-run)
- [Test email](#test-email)
- [GitHub Actions](#github-actions)
- [PostgreSQL em produção](#postgresql-em-produção)
- [Endpoints dinâmicos: como habilitar](#endpoints-dinâmicos-como-habilitar)
- [Adicionar uma nova empresa](#adicionar-uma-nova-empresa)
- [Testes](#testes)
- [Troubleshooting](#troubleshooting)
- [Limitações conhecidas](#limitações-conhecidas)

---

## Empresas monitoradas

Bluefit · Selfit · Bodytech · Planet Fitness · Xponential Fitness ·
Sports World · Basic-Fit · The Gym Group · PureGym · SATS · Benefit Systems ·
Leejam.

A Smart Fit não faz parte do monitoramento.

---

## O que gera e o que não gera alerta

A regra **não** é "encontrar novos PDFs" nem "encontrar earnings releases". É
monitorar as divulgações operacionais ou financeiras periódicas definidas
**individualmente** para cada empresa. Nenhuma regra de uma empresa é
generalizada para outra.

| Empresa | Gera alerta | Nunca gera alerta |
|---|---|---|
| Bluefit | Release de resultados (inclui "Comentário de Desempenho" e "Relatório da Administração") | Apresentações, DF/ITR/DFP, documentos CVM, transcrições, atas |
| Selfit | Demonstrações financeiras anuais | Assembleias/atas, avisos ao mercado, debêntures, agente fiduciário |
| Bodytech | Demonstrações financeiras anuais consolidadas | "Outras publicações legais", data de "última atualização" da página |
| Planet Fitness | `Announces ... Quarter ... Results` | `To Report ... Results`, `Key Year-End Metrics`, SEC filings, presentations, events |
| Xponential | Link rotulado `Earnings Release` | Webcast, audio, 10-Q, 10-K, XBRL, presentations, annual report |
| Sports World | Reportes Trimestrales (1T–4T) | Reportes BMV, Informes Anuales, XBRL, comunicados, webcasts |
| Basic-Fit | January TU, Q1 TU, Half Year Results, Q3 TU, Full Year Results | Capital Markets Day, annual reports, presentations, webcasts |
| The Gym Group | Pre-close trading update (jan/jul), Full Year Results, Interim Results | **`Notice of Pre-Close Trading Update`**, Annual Report and Accounts, site visits |
| PureGym | Report trimestral (Q1–Q4/FY) | Annual Reports; presentation/webcast/transcript isolados |
| SATS | `Qx Report YYYY` | `Annual Report YYYY`, **Pre-Close Call Scripts** |
| Benefit Systems | Relatórios **consolidados** FY/Q1/H1/Q3 + `Quarterly information on active sport cards' number` | Versões **standalone**, presentations, Finance Data, Operational Data, demais current reports |
| Leejam | `Interim/Annual Consolidated Financial Results` (Q1, Q2/H1, Q3/9M, Q4/FY) | Dividendos, aberturas de centros, assembleias, contratos, annual reports, transcripts |

O Financial Calendar, em qualquer empresa, serve apenas como sanity check e
para prever quando intensificar polling. **Nunca é gatilho de alerta.**

---

## Arquitetura

```
Scheduler (GitHub Actions, */15)
   ↓
CLI  →  runner  →  para cada empresa (isolada em try/except):
                        CompanyMonitor.fetch_candidates()
                             ↓
                        classify()      ← regras determinísticas (regex)
                             ↓
                        normalize()     ← NormalizedEvent + reporting_period
                             ↓
                        validate()
                             ↓
                        merge_events()  ← PDF+HTML, EN+ES, FS+Presentation
                             ↓
                   services/event_resolver.resolve()
                             ↓
                   evento novo?  ──NÃO──→ registra source_observation, log
                             │
                            SIM
                             ↓
                   services/alert_service.dispatch()  →  emailer.EmailSender
                             ↓
                   marca alert_sent_at
```

### Componentes

```
src/ir_monitor/
├── cli.py               CLI (bootstrap, check, inspect, send-test-email)
├── config.py            .env + config/companies.yaml
├── models.py            CandidateEvent, NormalizedEvent, EventType, RunSummary
├── normalization.py     períodos, canonicalização de URL, datas (puro, testável)
├── database.py          SQLAlchemy: events, source_observations, monitor_runs, meta
├── http.py              sessão compartilhada, timeout, retry, backoff, 429/5xx
├── emailer.py           interface EmailSender + SMTP + Console (dry-run)
├── logging_config.py    logging estruturado com redação de credenciais
├── util.py              timezone-aware clocks, validação de PDF (magic bytes)
├── monitors/
│   ├── base.py          CompanyMonitor + HTMLSourceMixin, RSSSourceMixin,
│   │                    PlaywrightFallbackMixin, EndpointProbeMixin, merge_events
│   └── <12 adapters>    lógica específica isolada por empresa
└── services/
    ├── event_resolver.py  dedup por event_key, enriquecimento de fontes
    ├── alert_service.py   um e-mail por evento lógico
    └── runner.py          orquestração, isolamento de falhas, bootstrap
```

Não existe nenhum `if company == ...` na lógica central. Adicionar uma empresa
é criar um arquivo em `monitors/`, registrá-lo em `monitors/__init__.py` e
adicionar uma entrada no YAML.

### Event key vs. document ID

São coisas distintas e a diferença é o que impede alertas duplicados:

- **Identificador técnico** (`document_identifier` / `technical_id`): UUID da
  MZ, GUID do RSS, release id, ou hash da URL canonicalizada. Identifica **um
  arquivo**.
- **Chave lógica** (`event_key`): identifica **a divulgação econômica**.
  - `company + event_type + reporting_period` — Basic-Fit, The Gym Group,
    Benefit Systems, Planet Fitness, Xponential.
  - `company + reporting_period` — Bodytech, Leejam, PureGym, SATS,
    Sports World, Bluefit, Selfit (onde o anexo pede dedup por período).

Um mesmo resultado que apareça em duas fontes, em PDF e HTML, em inglês e
espanhol, ou como Financial Statement + Earnings Presentation, converge para
**um** `event_key` e portanto **um** e-mail. As demais aparições viram linhas em
`source_observations`.

### Fail-safe contra mudanças de layout

O sistema detecta **adições positivas**, nunca remoções. Se uma página que
historicamente tem registros retornar zero itens, isso é tratado como
`ParserFailure` (layout change / bloqueio / parser quebrado), a empresa é
marcada como erro no summary e **nenhum alerta é gerado a partir de
desaparecimento de conteúdo**.

---

## Instalação local

```bash
git clone <repo-url> ir-watch
cd ir-watch

python3.12 -m venv .venv
source .venv/bin/activate

pip install -e .

# Opcionais, por empresa:
pip install -e ".[browser]"   # Playwright (fallback: Bluefit, Selfit, Bodytech,
python -m playwright install chromium   #  Basic-Fit, PureGym, Leejam)
pip install -e ".[pdf]"       # PyMuPDF (validação de conteúdo: Sports World,
                              #  Bodytech, fallback da Bluefit)
pip install -e ".[dev]"       # pytest, ruff

cp .env.example .env
# edite .env
```

Verifique a watch list carregada:

```bash
python -m ir_monitor list-companies
```

---

## Environment variables

| Variável | Default | Descrição |
|---|---|---|
| `DATABASE_URL` | `sqlite:///ir_watch.db` | SQLite em dev, PostgreSQL em produção |
| `SMTP_HOST` | — | Servidor SMTP |
| `SMTP_PORT` | `587` | `465` ativa SMTP_SSL automaticamente |
| `SMTP_USER` | — | Usuário SMTP (opcional se o relay não exigir auth) |
| `SMTP_PASSWORD` | — | Senha SMTP. **Nunca no código nem no repositório** |
| `SMTP_USE_TLS` | `true` | STARTTLS (ignorado na porta 465) |
| `EMAIL_FROM` | — | Remetente |
| `EMAIL_TO` | — | Destinatários separados por vírgula |
| `EMAIL_SUBJECT_PREFIX` | `[IR Watch]` | Prefixo do subject |
| `TIMEZONE` | `America/Sao_Paulo` | `detected_at`, logs e timestamps do e-mail |
| `ALERT_ON_REVISION` | `false` | Alertar quando um período conhecido é republicado |
| `PLAYWRIGHT_ENABLED` | `true` | Desligue em ambientes sem browser |
| `HTTP_TIMEOUT` | `30` | Timeout por request (s) |
| `HTTP_RETRIES` | `3` | Tentativas com backoff exponencial |
| `USER_AGENT` | `ir-watch/1.0 (...)` | UA identificável — coloque um contato real |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `text` | `text` ou `json` |
| `COMPANIES_FILE` | `config/companies.yaml` | |

O logger redige automaticamente senhas, tokens e a senha embutida em
`DATABASE_URL` antes de escrever qualquer linha.

---

## Bootstrap

No primeiro run, todas as páginas já têm dezenas de divulgações históricas.
O bootstrap registra tudo o que existe hoje como baseline e **não envia nenhum
e-mail**:

```bash
python -m ir_monitor bootstrap
```

Depois disso:

```bash
python -m ir_monitor check
```

alerta **somente** eventos que surgirem após o bootstrap.

O `check` **se recusa a rodar** contra um banco sem marcador de bootstrap:

```
error: database has no bootstrap marker. Run `python -m ir_monitor bootstrap`
first, or pass --allow-missing-bootstrap ...
```

Isso existe justamente para evitar o comportamento silencioso perigoso de
disparar o histórico inteiro por e-mail.

**Empresa adicionada depois do bootstrap**: como ela não tem nenhuma linha no
banco, seu primeiro `check` grava os eventos como baseline e emite um aviso
visível no summary (`NOTE: no prior history for this company`), em vez de enviar
o histórico completo. Rode `inspect` para conferir e o próximo `check` já alerta
normalmente.

---

## Dry run

```bash
python -m ir_monitor check --dry-run
python -m ir_monitor check --company planet_fitness --dry-run
```

No modo dry-run nenhum e-mail é enviado e nada é gravado em produção. O output
mostra exatamente o que teria acontecido:

```
planet_fitness  [ok   ]
  Source used:      planet_fitness_press_release_rss
  Candidates found: 20
  Relevant events:  8
  New events:       0
  Alerts:           0
```

Para ver os candidatos extraídos e sua classificação **sem tocar no banco**:

```bash
python -m ir_monitor inspect --company the_gym_group --verbose
```

---

## Test email

```bash
python -m ir_monitor send-test-email
```

Valida host, porta, TLS, autenticação e destinatários antes de você depender do
sistema em produção.

Formato do alerta real:

```
Subject: [IR Watch] Basic Fit — Trading Update — Q1-2026

Company:          Basic Fit
Event type:       Trading Update
Reporting period: Q1-2026
Publication date: 2026-04-22
Detected at:      22/04/2026 03:15 -03
Title:            Basic-Fit Q1 2026 Trading Update
Source:           basic_fit_results_endpoint
Primary link:     https://...
Document/PDF:     https://...

Additional material:
  Presentation: https://...
```

Enviado em HTML simples com alternativa plain text.

---

## GitHub Actions

O workflow `.github/workflows/monitor.yml` roda de hora em hora
(`cron: "23 * * * *"` — altere essa linha para mudar a cadência) e também
manualmente via **Run workflow**, com escolha de `mode`, `company` e `dry_run`.

### Cadastrar os secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Exemplo |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://user:pass@host:5432/irwatch` |
| `SMTP_HOST` | `smtp.office365.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `alerts@empresa.com` |
| `SMTP_PASSWORD` | *(senha ou app password)* |
| `EMAIL_FROM` | `alerts@empresa.com` |
| `EMAIL_TO` | `voce@empresa.com,outro@empresa.com` |

### Primeira execução

1. Cadastre os secrets.
2. **Run workflow** → `mode: bootstrap`. Nenhum e-mail é enviado.
3. Deixe o cron assumir a partir daí.

O job tem `concurrency` configurado para que duas execuções nunca corram sobre
o mesmo banco, e um passo de guarda que **falha explicitamente** se
`DATABASE_URL` estiver vazio ou apontando para SQLite.

---

## PostgreSQL em produção

O runner do GitHub Actions é descartável: um arquivo SQLite criado lá some
quando o job termina, e o run seguinte trataria **todas** as divulgações como
novas — dezenas de e-mails duplicados a cada 15 minutos.

Por isso o workflow recusa `sqlite://` e exige `DATABASE_URL` apontando para um
PostgreSQL persistente. A aplicação usa SQLAlchemy puro e não depende de nenhum
vendor: Supabase, Neon, Railway, RDS ou um Postgres próprio funcionam
igualmente. Basta que a URL siga o formato:

```
postgresql+psycopg://usuario:senha@host:5432/banco
```

O schema é criado automaticamente no primeiro `init_db()` (chamado por
`bootstrap` e `check`).

---

## Endpoints dinâmicos: como habilitar

Algumas empresas carregam os documentos dinamicamente (Bluefit/MZ,
Basic-Fit, PureGym/Q4, Selfit, Bodytech, Leejam/Saudi Exchange).

**O projeto nunca inventa uma URL de API.** As listas `candidate_endpoints` em
`config/companies.yaml` vêm **vazias** de propósito. Enquanto vazias, cada
adapter usa o fallback documentado para aquela empresa e continua funcionando.

Para habilitar o caminho estruturado (mais rápido, mais barato, mais robusto):

1. Abra a página no navegador com o DevTools em **Network → Fetch/XHR**.
2. Recarregue e localize a request que traz a lista de documentos.
3. Copie a URL completa (com query string).
4. Cole em `candidate_endpoints` da empresa:

```yaml
  basic_fit:
    candidate_endpoints:
      - https://corporate.basic-fit.com/<endpoint-real-confirmado>
```

5. Valide:

```bash
python -m ir_monitor inspect --company basic_fit --verbose
```

O adapter faz probe do endpoint, **valida o shape da resposta** e só então o
consome. Se o probe falhar por qualquer motivo (404, mudança de contrato, shape
inesperado), ele cai silenciosamente no fallback e loga
`action=endpoint_probe_failed`. O monitor nunca quebra por causa disso.

---

## Adicionar uma nova empresa

1. **Crie o adapter** em `src/ir_monitor/monitors/<empresa>.py`:

```python
from ..models import CandidateEvent, EventType, NormalizedEvent
from .base import CompanyMonitor, HTMLSourceMixin, candidate

class NovaEmpresaMonitor(HTMLSourceMixin, CompanyMonitor):
    key = "nova_empresa"
    min_expected_candidates = 1   # zero itens = parser failure, não "sem novidade"

    def fetch_candidates(self) -> list[CandidateEvent]:
        html = http.get_text(self.config.primary_url)
        self.source_used = "nova_empresa_html"
        return [...]

    def classify(self, cand):
        # regex determinístico; rejeições ANTES das aceitações
        return EventType.EARNINGS_RELEASE or None

    def normalize(self, cand, event_type):
        return NormalizedEvent(
            company=self.key,
            event_type=event_type,
            reporting_period=quarter_period(q, y),
            ...
            key_includes_event_type=False,  # se a dedup for por período
        )
```

2. **Registre** em `monitors/__init__.py`:

```python
from .nova_empresa import NovaEmpresaMonitor
REGISTRY["nova_empresa"] = NovaEmpresaMonitor
```

3. **Configure** em `config/companies.yaml`:

```yaml
  nova_empresa:
    name: Nova Empresa
    monitor: nova_empresa
    enabled: true
    primary_url: https://...
```

4. **Teste com fixture local** em `tests/fixtures/` — sem internet.

5. **Valide** com `inspect` e depois `check` (o auto-baseline evita o flood
   histórico da nova empresa).

Regras complexas ficam no adapter Python, não no YAML. O YAML guarda apenas
propriedades estáveis: URLs, toggles, candidate endpoints.

---

## Testes

```bash
pytest -q                       # 95 testes, offline
pytest --cov=ir_monitor -q
```

A suíte **não depende de internet**: usa fixtures HTML/RSS em
`tests/fixtures/` e um monitor fake. Cobre normalização de períodos,
classificação de títulos, criação de event keys, URL normalization,
deduplicação, bootstrap, detecção de eventos novos, comportamento de
republicação, isolamento de falhas e renderização de e-mail.

Os falsos positivos conhecidos têm testes dedicados e nomeados:

```
Planet Fitness   "To Report Second Quarter 2026 Results"       → NÃO relevante
Planet Fitness   "Announces Second Quarter 2026 Results"       → relevante
Planet Fitness   "Announces Key Year-End Metrics"              → NÃO relevante
The Gym Group    "Notice of Pre-Close Trading Update"          → NÃO relevante
The Gym Group    "Pre-close trading update"                    → relevante
Basic-Fit        "Q1 Trading Update"                           → relevante
Benefit Systems  "Quarterly information on active sport
                  cards' number"                               → relevante
Benefit Systems  Consolidated                                  → relevante
Benefit Systems  Standalone                                    → sem segundo alerta
SATS             "Pre-Close Call Script"                       → NÃO relevante
SATS             "Annual Report 2025"                          → NÃO relevante
```

Para checks reais contra as fontes (opcional, exige rede):

```bash
python -m ir_monitor check --company planet_fitness --dry-run
```

---

## Troubleshooting

### A página mudou / `parser_failure`

```
ERROR company=sats action=parser_failure error="sats: expected at least 1
candidate(s), got 0 - treating as parser/layout failure"
```

Isso é **intencional**: zero registros numa página historicamente populada
nunca é interpretado como "sem novidade". Diagnóstico:

```bash
python -m ir_monitor inspect --company sats --verbose
```

Se a URL mudou, ajuste `primary_url` no YAML. Se a estrutura mudou, o parser
casa por **texto do link e padrão de href**, não por classes CSS — geralmente
basta ajustar o regex de rótulo no adapter.

### O endpoint parou de funcionar

Log: `action=endpoint_probe_failed` ou `action=endpoint_probe_shape_mismatch`.
O adapter já caiu para o fallback e continua funcionando. Reconfirme o endpoint
no DevTools e atualize `candidate_endpoints`, ou apenas esvazie a lista.

### Playwright

```
ParserFailure: Playwright is not installed; run `playwright install chromium`
```

```bash
pip install -e ".[browser]" && python -m playwright install --with-deps chromium
```

Em ambiente sem browser, `PLAYWRIGHT_ENABLED=false`. As empresas que dependem
dele como fallback passam a reportar erro em vez de travar o run — as outras
continuam normalmente.

### HTTP 403 / 429

O cliente já trata 429 (respeita `Retry-After`) e 5xx com backoff exponencial.
Para 403 persistente: confirme que `USER_AGENT` tem um contato real e reduza a
frequência do cron. **O projeto não implementa e não deve implementar
contorno de CAPTCHA ou proteção anti-bot** — se uma fonte passar a exigir isso,
desabilite a empresa (`enabled: false`) e reavalie a fonte.

### SMTP

```bash
python -m ir_monitor send-test-email
```

- `SMTP failure: SMTPAuthenticationError` → credenciais; provedores com MFA
  exigem app password.
- Porta 465 usa SSL implícito automaticamente; 587 usa STARTTLS.
- Nada é logado em claro: o filtro de redação cobre senhas e tokens.

### Database

- `BootstrapRequired` → rode `bootstrap` primeiro.
- Conexão Postgres → instale `psycopg[binary]` e confira o formato
  `postgresql+psycopg://`.
- Alertas duplicados após um deploy → quase sempre é banco efêmero. Confirme
  que `DATABASE_URL` aponta para storage persistente.

---

## Limitações conhecidas

Registradas honestamente, sem afirmar que algo funciona sem ter sido validado:

1. **Nenhum endpoint dinâmico foi validado no ambiente de construção.** Não foi
   possível inspecionar tráfego XHR. Por isso todas as listas
   `candidate_endpoints` estão vazias e cada empresa opera pelo fallback
   documentado. Habilitá-las é um passo manual de 2 minutos descrito acima, e
   melhora robustez e custo de requests.

2. **Bluefit**: confirmado que `ri.bluefit.com.br` é MZ e renderiza
   client-side (HTTP puro retorna "Nenhum arquivo para o ano selecionado").
   O caminho funcional hoje é Playwright. O UUID MZ é extraído da URL
   `mzfilemanager/v2/d/{site}/{documento}` como identificador técnico.

3. **PureGym**: a URL do anexo
   (`/investor/financial-results/quarterly-results/`) parece desatualizada; a
   página viva é `/investors/results-reports-and-presentations/default.aspx`.
   Ambas são tentadas. Plataforma Q4 Inc. confirmada (CDN `s28.q4cdn.com/583314398`),
   e o parser reconhece o padrão `/doc_financials/{ano}/{q}/` para derivar o
   período direto da URL do documento.

4. **Bodytech / SPED**: a Central de Balanços exige um fluxo de busca
   interativo e protegido. O adapter **pula a fonte com warning explícito** em
   vez de tentar qualquer contorno. A fonte do site mantém o monitor funcional,
   e como a chave lógica é `company + FY`, ligar o SPED depois não gera alerta
   duplicado para um exercício já notificado.

5. **Sports World**: o probe preditivo (`gsw_reporte_{PERIODO}.pdf`) é uma
   **segunda camada**, nunca a única fonte — o padrão de nomes pode mudar. Todo
   PDF probado é validado por magic bytes `%PDF` + checagem determinística de
   período, menção a Grupo Sports World e linguagem de resultados. Sem PyMuPDF
   instalado, a validação cai para magic bytes + período no filename.

6. **Basic-Fit — January Trading Update**: normalizado como `JAN-TU-YYYY` pelo
   ano de publicação, conforme o anexo. Note que economicamente ele reporta
   números do exercício anterior; se preferir `JAN-TU-(YYYY-1)`, é uma linha em
   `basic_fit_period()`.

7. **The Gym Group / Basic-Fit — inferência de período por mês de publicação**:
   quando o título não traz o ano explícito, o período é derivado do mês de
   publicação (pre-close de janeiro → `FY_PRE_CLOSE-(YYYY-1)`, FY de março →
   `FY-(YYYY-1)`). Uma divulgação publicada fora da janela habitual pode cair no
   período vizinho. O `inspect` mostra o período atribuído antes de qualquer
   e-mail.

8. **PDF text extraction** é opcional (`pip install -e ".[pdf]"`). Sem ela, os
   fallbacks de validação por conteúdo (Bluefit, Bodytech, Sports World)
   degradam para validação por metadados e magic bytes.
