# Guia de instalação — para quem nunca programou

Você não vai instalar nada no seu computador. Não vai abrir terminal.
Tudo acontece dentro do site do GitHub, clicando.

Tempo estimado: **20 a 30 minutos** na primeira vez.

---

## Antes de começar: o que são essas coisas

Três palavras que vão aparecer o tempo todo:

**GitHub** — um site onde as pessoas guardam código. Pense numa Google Drive
para programas. É gratuito.

**Repositório** (ou "repo") — uma pasta dentro do GitHub. Você vai criar uma
pasta chamada `ir-watch` e colocar os arquivos do projeto lá dentro.

**Workflow** (ou "Action") — uma tarefa que o GitHub executa sozinho, de tempos
em tempos, nos computadores dele. É isso que vai rodar o monitor de hora em
hora, mesmo com seu notebook desligado.

**Issue** — um post dentro do repositório. Quando o monitor achar uma
divulgação nova, ele abre um post desses, e o GitHub te manda um e-mail
avisando. É assim que o alerta chega até você.

---

## Passo 1 — Criar a conta no GitHub

Se você já tem, pule.

1. Abra `github.com`
2. Clique em **Sign up**
3. Use um e-mail que você lê com frequência. **É nesse e-mail que os alertas
   vão chegar.**
4. Confirme o e-mail que o GitHub enviar.

O plano gratuito é suficiente. Não é pedido cartão de crédito.

---

## Passo 2 — Criar o repositório

1. Com a conta aberta, vá em `github.com/new`
2. **Repository name**: digite `ir-watch`
3. **Description**: pode deixar vazio
4. Escolha **Public**

   > Por que público? Repositórios públicos têm execução ilimitada e gratuita.
   > Privados têm um limite mensal que o monitor de hora em hora provavelmente
   > estouraria. O código não contém nenhuma senha sua, e a lista de empresas
   > é de companhias abertas.
   >
   > Se a Smart Fit tiver política contra repositório público, pare aqui e fale
   > com o time de TI antes de continuar.

5. **NÃO** marque "Add a README file"
6. Clique em **Create repository**

Você vai cair numa página com instruções em fundo escuro. Ignore tudo isso.

---

## Passo 3 — Colocar os arquivos lá dentro

1. Descompacte o arquivo `ir-watch-simples.zip` que você baixou.
   Vai virar uma pasta chamada `ir-watch-simples`.

2. **Abra a pasta** e olhe o que tem dentro. Você deve ver:
   `README.md`, `GUIA.md`, `WORKFLOW-PARA-COLAR.txt`, `pyproject.toml`,
   e as pastas `config`, `src`, `tests`.

3. De volta ao GitHub, na página do repositório recém-criado, clique no link
   **uploading an existing file** (fica no meio do texto).

4. Selecione **todos** os itens de dentro da pasta e arraste para a área do
   navegador.

   > Atenção: arraste o **conteúdo** da pasta, não a pasta inteira.
   > Selecione tudo com Ctrl+A (Windows) ou Cmd+A (Mac) e arraste.

5. Espere aparecer a lista de arquivos. Role até o fim.

6. Em **Commit changes**, escreva `primeira versao` e clique em
   **Commit changes**.

Se aparecer erro em algum arquivo, tudo bem — o passo 4 resolve o mais
importante.

---

## Passo 4 — Criar o arquivo que faz o robô rodar

Este arquivo mora numa pasta escondida (`.github`), que o Windows e o Mac não
mostram por padrão. Por isso ele provavelmente não subiu no passo anterior.
Vamos criá-lo à mão.

1. Na página principal do repositório, clique em **Add file** (botão perto do
   verde, no alto à direita) → **Create new file**

2. No campo do nome do arquivo, digite **exatamente** isto:

   ```
   .github/workflows/monitor-simple.yml
   ```

   > Repare que ao digitar as barras `/` o GitHub vai criando as pastas
   > sozinho. Isso é normal e é o que queremos.

3. Abra o arquivo `WORKFLOW-PARA-COLAR.txt` (que está na pasta que você
   descompactou) com o Bloco de Notas ou o TextEdit.

4. Selecione **todo** o conteúdo dele (Ctrl+A / Cmd+A), copie (Ctrl+C / Cmd+C)
   e cole na área grande do GitHub.

5. Role até o fim e clique em **Commit changes** → **Commit changes**.

---

## Passo 5 — Dar permissão para o robô trabalhar

Sem isso ele não consegue salvar o que já viu nem te avisar.

1. No repositório, clique em **Settings** (engrenagem, na barra de cima)
2. No menu da esquerda: **Actions** → **General**
3. Role até o fim, na seção **Workflow permissions**
4. Marque **Read and write permissions**
5. Clique em **Save**

---

## Passo 6 — Ligar os e-mails

1. Volte para a página principal do repositório (clique no nome `ir-watch` no
   alto)
2. No alto à direita tem um botão **Watch** (com um olho). Clique nele.
3. Escolha **All Activity**

Agora confirme que o GitHub tem permissão de te mandar e-mail:

4. Vá em `github.com/settings/notifications`
5. Na seção **Subscriptions → Watching**, garanta que **Email** está marcado

---

## Passo 7 — A primeira execução (importante!)

Neste momento, todas as 12 empresas já têm dezenas de divulgações antigas
publicadas. Se você simplesmente ligar o robô, ele vai te mandar dezenas de
alertas de coisas velhas.

Para evitar isso, existe uma primeira execução especial chamada **bootstrap**.
Ela olha tudo o que existe hoje, anota como "já conhecido", e **não avisa
nada**. Depois disso, só o que for novo gera alerta.

1. Clique em **Actions** (na barra de cima do repositório)
2. Se aparecer um aviso amarelo pedindo para habilitar workflows, clique no
   botão verde para habilitar
3. No menu da esquerda, clique em **IR Watch (simple)**
4. À direita aparece **Run workflow**. Clique.
5. No campo **mode**, escolha **bootstrap**
6. Clique no botão verde **Run workflow**

Espere de 1 a 3 minutos. A execução aparece na lista. Clique nela para
acompanhar.

**Ao terminar você deve ver uma bolinha verde ou laranja.** Laranja significa
que algumas empresas falharam — isso é esperado neste momento (veja
"O que esperar" mais abaixo).

---

## Passo 8 — Conferir que ficou certo

1. Ainda em **Actions** → **IR Watch (simple)** → **Run workflow**
2. Desta vez deixe **mode** em `check` e marque **dry_run** como `true`
3. Rode

Clique na execução, abra o passo **Run monitor** e procure no fim um resumo
assim:

```
--- totals ---
Companies checked: 12
Successes:         6
Errors:            6
Events found:      41
New events:        0      <-- este é o número que importa
Alerts sent:       0
```

**`New events: 0` é o que você quer ver.** Significa que o bootstrap funcionou
e o robô já conhece tudo o que existe hoje.

Se aparecer um número maior que zero aqui, me avise antes de seguir.

Pronto. A partir de agora ele roda sozinho, de hora em hora.

---

## O que esperar nas primeiras semanas

### Metade das empresas vai dar erro. Isso é esperado.

Seis empresas — **Bluefit, Selfit, Bodytech, Basic-Fit, PureGym e Leejam** —
carregam os documentos de um jeito que exige um recurso mais pesado, desligado
nesta configuração simples para o robô rodar rápido.

As outras seis funcionam desde o primeiro dia: **Planet Fitness, Xponential,
Sports World, The Gym Group, SATS e Benefit Systems**.

Para recuperar as seis que faltam é preciso um ajuste técnico (está descrito no
`README.md`, seção "Endpoints dinâmicos"). É uma tarefa de uns 30 minutos para
alguém que programa, e vale a pena pedir ajuda.

### Quando chegar um alerta

Você recebe um e-mail do GitHub com o assunto tipo:

```
[ir-watch] Planet Fitness — Earnings Release — Q3-2026
```

Dentro tem empresa, tipo de evento, período, data de publicação e o link
direto para o documento.

### Quando alguma coisa quebrar

Você vai ver um X vermelho em **Actions**. Abra a execução, procure a linha que
começa com `ERROR company=`. Ela diz exatamente qual empresa falhou e por quê.

**Uma empresa quebrada nunca impede as outras 11.** O sistema foi feito assim
de propósito.

O sinal mais comum é `parser_failure`, que significa "a página mudou de
layout". É a hora de pedir ajuda técnica — leve o texto do erro junto.

### O que nunca vai acontecer

O sistema **nunca** te avisa por causa de conteúdo que sumiu de uma página.
Ele só avisa quando algo **novo** aparece. Se uma página quebrar e voltar
vazia, isso vira erro, não alerta falso.

---

## Uma alternativa que talvez sirva melhor

Vale você saber que existe um caminho sem nenhuma tecnologia:

Planet Fitness, Xponential, The Gym Group, PureGym, SATS e Basic-Fit oferecem
**cadastro de alerta por e-mail direto no site de RI delas** ("Email Alerts" ou
"Investor Alerts"). São 10 minutos de cadastro e cobre metade da watch list
sem nada para manter.

O que você perde: recebe **todos** os comunicados, não só as divulgações
periódicas (vai vir dividendo, assembleia, mudança de conselho junto). E não
cobre Bluefit, Selfit, Bodytech, Sports World, Benefit Systems e Leejam, que
não têm esse recurso.

Uma combinação razoável: cadastre os alertas nativos hoje para ter cobertura
imediata, e use este projeto para as empresas que não têm alternativa — que é
exatamente onde ele agrega mais.
