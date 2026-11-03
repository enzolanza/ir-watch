# IR Watch

Robô que avisa quando **Bluefit**, **Selfit** ou **Bodytech** publicam novas
demonstrações financeiras. O aviso chega como uma *issue* neste repositório,
e o GitHub manda um e-mail sobre ela.

## O que cada empresa publica e onde o robô olha

| Empresa | O que gera alerta | Onde o robô olha | Frequência |
|---|---|---|---|
| Bluefit | Demonstrações financeiras trimestrais (ITR) e anuais (DFP) | Catálogo de documentos da "Central de Resultados" do site de RI | 4 vezes por ano |
| Selfit | Demonstrações financeiras anuais | Seção "Demonstrações financeiras anuais" da página de investidores | 1 vez por ano |
| Bodytech | Demonstrações financeiras anuais | Item "Demonstrações financeiras" da página "Políticas e documentos" | 1 vez por ano |

A Bluefit não publica "release de resultados", por isso o alerta dela é
sobre as demonstrações financeiras.

## Como funciona

1. A cada 30 minutos o GitHub executa o robô (aba **Actions**, "IR Watch (simple)").
   Em dias de muito movimento o GitHub pode atrasar algumas execuções.
2. O robô lê a lista de documentos de cada empresa e compara com o que já conhece
   (guardado em `state/ir_watch.db`).
3. Documento novo → abre uma issue com o link do PDF. Cada documento gera **um** alerta só.

## As proteções

- **Aviso "sem monitoramento":** se o robô não conseguir ler uma empresa em
  3 execuções seguidas, ele abre uma issue "⚠️ <empresa> sem monitoramento".
  Enquanto ela estiver aberta, confira o site da empresa manualmente.
  Quando a empresa voltar a funcionar, a issue é encerrada sozinha.
- **Nunca "nada de novo" por engano:** se uma lista vier vazia ou diferente do
  esperado, o robô trata como falha (e avisa), não como silêncio.
- **Alerta que falhou é reenviado** na execução seguinte.
- **Primeira leitura de uma empresa:** tudo que já está publicado vira histórico
  (sem alerta), e o robô abre uma issue "Linha de base registrada — <empresa>"
  listando o documento mais recente para você conferir no site.

## Para receber os e-mails

No topo deste repositório: **Watch → All Activity**. Confira também se o e-mail
de notificações do GitHub é o que você usa (GitHub → Settings → Notifications).

## Testar sem risco

Aba **Actions → IR Watch (inspect validation) → Run workflow**. Esse teste só
lê os sites e mostra o que o robô encontrou em cada empresa. Não grava nada e
não abre alertas. Use antes de aprovar qualquer mudança no robô.

## Ligar ou desligar empresas

Em `config/companies.yaml`, cada empresa tem `enabled: true` (ligada) ou
`enabled: false` (desligada). As outras 9 empresas da versão 1 continuam lá,
desligadas. A versão 1 completa está guardada na branch `v1-backup`.

## Para quem programa

```bash
pip install -e ".[dev]"
python -m pytest                              # testes (offline)
python -m ir_monitor inspect --company bluefit --verbose
python -m ir_monitor check --dry-run          # simula, não grava nada
```

Código das empresas: `src/ir_monitor/monitors/{bluefit,selfit,bodytech}.py`.
Lógica de execução e proteções: `src/ir_monitor/services/runner.py`.
