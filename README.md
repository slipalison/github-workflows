# github-workflows

Esteira de CI/CD reutilizável. Um repositório de aplicação chama **um** workflow
e recebe lint, testes, cobertura com piso, imagem com portão de vulnerabilidade,
sete varreduras de segurança, Sonar com Quality Gate, a versão semântica
decidida pelos commits, e o commit no GitOps — com tudo o que pode ser paralelo
rodando em paralelo.

```yaml
jobs:
  esteira:
    permissions: { contents: write, packages: write, security-events: write, actions: read }
    uses: slipalison/github-workflows/.github/workflows/pipeline.yml@main
    with:
      componentes: '[{"nome":"app","linguagem":"python","cobertura":80}]'
      imagem: ghcr.io/slipalison/meu-app
      app: meu-app
      sonar_projeto: slipalison_meu-app
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
      GITOPS_SSH_KEY: ${{ secrets.GITOPS_SSH_KEY }}
```

Exemplos completos e comentados em [`exemplos/`](exemplos/): Python simples,
repositório poliglota .NET + dois frontends, monorepo Rust + React numa imagem
só, e repositório privado.

---

## O desenho

```
        ┌─ versao     (próxima versão, pelos commits)
        ├─ qualidade  (N componentes, N jobs paralelos)
push ───┼─ imagem     (build → varre → publica)          ─── portão ─── publicar ─── lancar
        ├─ sonar      (análise + Quality Gate)                          (GitOps)    (alias, tag, release)
        └─ seguranca  (6 varreduras num job)                                 só na main
```

O relógio é o do job mais lento, não a soma.

### A mudança que mais importa

A esteira anterior daqui era `testes → imagem → publicar`. Três etapas em fila,
e a do meio esperava por um motivo que não se sustenta: `docker build` não
depende de `pytest`. O que não pode acontecer é imagem de código reprovado
**chegar ao cluster** — e quem impede isso é o `publicar`, não o `imagem`. Uma
tag `sha-<commit>` no registro de um commit que falhou não machuca ninguém:
ninguém a referencia, e o GitOps nunca a aponta.

Medido no `demo-python` em 2026-09-13, e o resultado não é o que a intuição
sugere:

| | Jobs | Relógio |
|---|---|---|
| Esteira antiga, em fila | 3 | **78s** |
| Esteira nova, em paralelo | 13 | **100s** |

A nova é 22 segundos **mais lenta**, e faz sete varreduras de segurança, Sonar e
piso de cobertura que a antiga não fazia. Somados em fila, esses mesmos jobs
dariam ~5 minutos.

O paralelismo não encurtou o que já existia — ele absorveu o que faltava. Esse
é o ganho real, e vale dizer assim em vez de anunciar um número menor.

O job `imagem` sozinho subiu de 43s para 79s, de propósito: ele agora constrói,
varre e **só então** publica.

### A tag de preview: `pr-<número>-<7 do head>`

Num pull request, a imagem sai com **dois nomes para o mesmo digest**:
`sha-<merge commit>` e `pr-<número>-<7 primeiros do SHA do head do PR>`. O
segundo existe para o ambiente de preview do cluster (o ApplicationSet
`previews` do `homelab-gitops`, desenho em
`distros-setup/proxmox/PLANO-PREVIEW.md`): o gerador de pull request do ArgoCD
só conhece o SHA do head, e o `sha-` do PR é o do merge commit que o GitHub
monta. Sem um nome que os dois lados sabem calcular, o preview pediria uma tag
que não existe.

Duas coisas que valem saber:

- **O preview roda o merge commit**, não o head sozinho — é o que entra na
  `main` se o PR for aceito;
- **a frase de cima deixa de valer para esta tag**: o `imagem` corre em
  paralelo com os testes, então um preview pode subir com código que a
  `qualidade` reprovou. É o comportamento desejado num ambiente de validação;
  o que continua barrando é o portão do Trivy, que vem **antes** do push.

---

## O que veio da análise dos dois repositórios

O pedido foi olhar
[`simulator-ccb/.github/workflows/ci.yml`](https://github.com/slipalison/simulator-ccb/blob/master/.github/workflows/ci.yml)
e [`TranslateReader/.github/workflows`](https://github.com/slipalison/TranslateReader/tree/main/.github/workflows).
São dois extremos úteis: um monólito de 1296 linhas e um conjunto de doze
arquivos pequenos.

### `simulator-ccb` — o que estava certo, e o que custava caro

Certo: as varreduras de segurança já rodavam em paralelo, `permissions:
read-all` no topo, e um resumo por job no painel do run — que é uma prática boa
e rara.

O que foi corrigido aqui:

**`needs` que não economiza nada.** `backend-tests` declara `needs:
backend-build`, mas `backend-build` não sobe artefato nenhum. O job de teste
refaz `restore` e `build` do zero. O `needs` ali não reaproveita trabalho: ele
apenas **serializa dois builds idênticos**. São três pares (backend, dois
frontends), seis builds, três esperas — para nada. Aqui build e teste vivem no
mesmo job, e o que corre em paralelo são os componentes, que de fato não
dependem uns dos outros.

**~600 linhas de `echo`.** Cada job de segurança termina montando a mesma tabela
à mão no `$GITHUB_STEP_SUMMARY`, e antes disso chama quatro `python3 -c` de uma
linha para contar achados no mesmo JSON. Dez jobs, dez cópias. Só que todas
aquelas ferramentas falam **SARIF** — então ler, contar e desenhar a tabela é
*um* problema. Virou [`bin/resumo_sarif.py`](bin/resumo_sarif.py), chamado por
uma composite action.

**O portão de cobertura dentro do YAML.** Sessenta linhas de bash chamando um
Python embutido que devolve seis números por posição de linha (`sed -n '1p'`,
`sed -n '2p'`…), depois `bc -l` para comparar. Não roda fora do CI, não se
testa, e a única forma de saber se funciona é quebrar um pull request. Virou
[`bin/cobertura.py`](bin/cobertura.py), que roda no terminal e é exercitado nas
duas cores pelo CI deste repositório.

**Actions por tag móvel** (`@v6`, `@v5`, `@v3`) — ver a seção de segurança
abaixo.

**Sem `concurrency`.** Três pushes seguidos rodavam três esteiras completas em
paralelo.

### `TranslateReader` — o que foi copiado

Este é o repositório maduro dos dois, e três coisas vieram dele quase inteiras:

**O guard contra o pulo silencioso**, e é a melhor peça do conjunto.
`${{ secrets.SONAR_TOKEN }}` vazio **não dá erro**: os passos são pulados, o job
fica verde, e o Quality Gate — que é check obrigatório — passa sem ter analisado
uma linha. O controle desaparece sem deixar rastro. O guard falha alto onde o
token deveria existir e avisa onde a ausência é legítima (fork, Dependabot).
Está generalizado em [`sonar.yml`](.github/workflows/sonar.yml).

É a mesma família de falha que este projeto já pagou duas vezes no cluster: o
webhook do OpenTelemetry com `failurePolicy: Ignore`, e a NetworkPolicy que
cortou o Alloy. Nos três casos o painel ficava verde.

**`sonar.qualitygate.wait=true` na fase `begin`**, e não na `end`: o
SonarScanner for .NET recusa a chave no `end` com *"This setting is not valid in
the end phase in this version of the C# plugin"* e sai 1 sem sequer consultar o
portão — o que parece reprovação e não é.

**`harden-runner`, SHA em todo `uses:`, `persist-credentials: false`.**

O que **não** veio: a duplicação. O mesmo preâmbulo de quatro passos aparece nos
doze arquivos, e a mesma versão do `harden-runner` está fixada em doze lugares —
atualizar é editar doze arquivos e torcer para não esquecer nenhum, e um
esquecido não dá erro, só deixa de ter a proteção. Aqui isso é
[uma composite action](.github/actions/preparar/action.yml) e
[um arquivo de versões](actions.lock.json).

---

## Segurança

| Controle | Onde | Por quê |
|---|---|---|
| Todo `uses:` fixado por **SHA** | [`actions.lock.json`](actions.lock.json) + [`bin/pinar_actions.py`](bin/pinar_actions.py) | Uma tag é ponteiro móvel. `@v4` não diz qual código roda — diz o que o dono daquele repositório está chamando de v4 hoje, e esse código roda com o `GITHUB_TOKEN` do run e vê os secrets do job. Foi assim que o `tj-actions/changed-files` alcançou milhares de pipelines em 2025. |
| `harden-runner` em todo job | [`preparar`](.github/actions/preparar/action.yml) | Registra (ou bloqueia) a saída de rede do runner. No `deploy.yml` é `block` com lista fechada — é o único job que segura credencial de escrita em outro repositório. |
| `persist-credentials: false` | idem | Por padrão o checkout deixa o token do run gravado em `.git/config`, legível por qualquer passo seguinte, inclusive por script de dependência de terceiro. |
| Valores dinâmicos por `env`, nunca por interpolação dentro de `run:` | [`sonar.yml`](.github/workflows/sonar.yml) | `${{ }}` é substituído no **texto** do script antes de o bash ver a primeira linha. `pull_request.head.ref` é um nome de branch — escolhido por quem abre o PR, inclusive de um fork. Por `env`, o valor é dado; por interpolação, pode virar comando. |
| `permissions` mínimo, por job | todos | Um workflow reutilizável nunca recebe mais permissão do que o chamador concede. |
| Imagem varrida **antes** de publicar | [`build-push.yml`](.github/workflows/build-push.yml) | A versão anterior publicava e só depois varria: uma imagem com CRITICAL ficava no GHCR mesmo com o job vermelho. |
| Segredo varrido no **histórico inteiro** | [`seguranca.yml`](.github/workflows/seguranca.yml) | Um segredo removido do HEAD continua em qualquer clone. Achado ali significa **rotacionar**, não apagar a linha. |
| `concurrency` com `cancel-in-progress` | exemplos | Impede que um run obsoleto ainda escreva no GitOps. |
| Tag e release só com `GITHUB_TOKEN`, no último job | [`lancar.yml`](.github/workflows/lancar.yml) | `contents: write` existe num job só, depois de todos os portões, e nunca em pull request. Nenhum PAT: tudo o que precisa da versão acontece no mesmo run. Assunto de commit é texto de terceiro — entra nas notas por arquivo, nunca por linha de comando. |

O CI deste repositório roda `pinar_actions.py --verificar`, que **falha** se
algum `uses:` escapar por tag ou divergir do lock.

Atualizar as actions:

```bash
python bin/pinar_actions.py --atualizar   # resolve as versões novas e reescreve
git diff                                  # ler ANTES de commitar
```

A leitura do diff não é formalidade: atualizar action é trocar código de
terceiro que roda com os seus secrets.

---

## Sonar para todo mundo, público e privado

O portão é o mesmo nos dois casos; o que muda é onde a análise roda, e a razão é
licença.

| | Público | Privado |
|---|---|---|
| **Sonar** | SonarQube Cloud, gratuito | Cloud é pago → `sonar_host` aponta para instância própria |
| **SARIF na aba Security** | gratuito | exige GitHub Advanced Security (pago) |
| **CodeQL** | gratuito | idem |

Com `publicar_sarif: auto` (o padrão), o envio do SARIF é detectado e pulado em
repositório privado — o que evita o 403 — **sem desligar controle nenhum**: o
resultado continua no painel de cada job, escrito por `resumo_sarif.py`, e os
portões continuam derrubando o run. Perde-se a aba, não a verificação.

O que **não** se faz é deixar um repositório privado sem análise e fingir que o
portão existe. Para isso há `sonar_exigir_token: false`, que é explícito, aparece
no diff, e emite `::warning::` em todo run.

### Preparar um repositório do zero, num comando

[`bin/preparar_repositorios.py`](bin/preparar_repositorios.py) faz a sequência
inteira: gera a chave `ed25519`, sobe como **deploy key com escrita** no
repositório de GitOps, grava `GITOPS_SSH_KEY` e `SONAR_TOKEN`, e roda a
conferência no fim.

```bash
python bin/preparar_repositorios.py --tudo                       # gera a chave e grava tudo
python bin/preparar_repositorios.py --secrets --chave ~/.ssh/gitops_ci   # a chave já existe
python bin/preparar_repositorios.py --chave-nova                 # só a deploy key
python bin/preparar_repositorios.py --conferir                   # só o relatório
```

**Ele não reimplementa o semeador** — chama o `semear_secret.py` como
subprocesso. Descoberta, impressão digital, validação na origem e a regra de o
valor ir por stdin continuam morando num lugar só.

Nasceu de dois tropeços reais, os dois na fronteira entre bash e PowerShell:
`chmod`, que não existe no Windows, e `-N ""`, que o PowerShell entrega ao
`ssh-keygen` como uma **passphrase literal de dois caracteres** — o que produz
uma chave que o Actions não consegue usar, falhando lá na frente com um erro que
não diz isso. Aqui os argumentos vão por lista, sem shell no meio, e o problema
deixa de existir nos dois sistemas.

O token continua **nunca vindo por argumento**: variável de ambiente, arquivo,
ou digitado sem eco. `argv` é legível por qualquer processo da máquina. Os
arquivos temporários ficam num diretório que some no `finally`, mesmo quando
algo falha no meio.

### O secret em vários repositórios de uma vez

Não existe secret de Actions global para conta pessoal — o `gh` é explícito:
secret de `user` serve ao Codespaces, não ao Actions. E secret de **organização,
no plano Free, não alcança repositório privado**: a organização não elimina o
problema, divide em dois.

[`bin/semear_secret.py`](bin/semear_secret.py) resolve os dois com um comando:

```bash
# O QUE FALTA, EM QUEM. Não grava nada. Sai 1 se faltar algum — serve de
# tarefa agendada ou de passo de CI.
python bin/semear_secret.py --conferir

python bin/semear_secret.py --listar --secret SONAR_TOKEN   # quem receberia
SONAR_TOKEN=... python bin/semear_secret.py --aplicar --secret SONAR_TOKEN

# Vários numa passada: um arquivo por secret, com o NOME do secret como nome
# do arquivo, num diretório FORA de qualquer repositório.
python bin/semear_secret.py --aplicar     --secret SONAR_TOKEN --secret NPM_TOKEN     --de-diretorio ~/.config/segredos-github

# TODOS os repositórios da conta, e não só os que usam o secret.
python bin/semear_secret.py --listar  --todos --secret NPM_TOKEN   # a lista, sem gravar
NPM_TOKEN=... python bin/semear_secret.py --aplicar --todos --secret NPM_TOKEN
```

**`--todos` grava onde o secret não é usado, e isso é escolha de quem roda.**
O padrão continua sendo a descoberta, porque uma cópia a mais é mais um lugar
de onde o segredo pode vazar e mais um para lembrar de trocar na rotação — com
82 repositórios, são 82 cópias. O modo existe para quem prefere gravar uma vez
em tudo a voltar aqui a cada repositório novo. Ele mostra a conta na tela e
**pede confirmação digitada**; `--listar --todos` mostra a lista antes, e
`--sim` pula a pergunta em script. Com `--todos` o secret também não precisa
estar em `PADROES` — não há o que descobrir.

Os secrets que ele conhece, e como cada um encontra o próprio público:

| Secret | Quem consome | Validado antes de gravar |
|---|---|---|
| `SONAR_TOKEN` | quem chama o `sonar.yml` ou passa `sonar_projeto` | `GET /api/users/current` do SonarCloud |
| `GITOPS_SSH_KEY` | quem chama o `deploy.yml` ou o `pipeline.yml` | — |
| `GITOPS_TOKEN` | idem — **alternativa** à chave SSH, não um segundo requisito | — |
| `NPM_TOKEN` | quem referencia `secrets.NPM_TOKEN` no próprio workflow | `GET /-/whoami` do registry |
| `GH_PACKAGES_TOKEN` | quem referencia `secrets.GH_PACKAGES_TOKEN` no próprio workflow | `GET /user`, exigindo o escopo `read:packages` |

O `NPM_TOKEN` e o `GH_PACKAGES_TOKEN` são os únicos cujo padrão **não** é o
caminho desta esteira: quem publica no npm tem workflow próprio, e quem lê
pacote privado declara o secret no `ci.yml`. O critério vira a referência ao
próprio secret, que é o mais honesto que existe — se um workflow o lê, aquele
repositório precisa dele.

**Os dois têm nome separado de propósito, e não por gosto.** `NPM_TOKEN`
**publica** no npmjs; `GH_PACKAGES_TOKEN` **lê** do npm do GitHub. Um nome só
para os dois faria a próxima rotação gravar o token de publicação onde se
espera o de leitura — e o estrago apareceria meses depois, num `npm ci` com 401
dentro de um `docker build`, onde a mensagem do npm nem diz qual token faltou.

**Alternativas contam como atendidas.** `GITOPS_SSH_KEY` e `GITOPS_TOKEN` se
substituem: o `deploy.yml` usa o que encontrar. Sem essa tabela o `--conferir`
acusava o `demo-python` de estar sem `GITOPS_TOKEN`, que ele não precisa ter —
e um relatório que aponta problema onde não há é um relatório que se aprende a
ignorar.

Três decisões dentro dele, e as três vieram de erro medido:

**A lista não é mantida à mão.** Ele lê os workflows de cada repositório e
descobre quem consome aquele secret. Uma lista escrita à mão envelhece calada —
alguém adota a esteira, esquece de acrescentar, e na próxima rotação aquele
repositório fica com o token velho.

**Faltar um secret precisa ter como perguntar.** Até 2026-09-16 a única forma
de descobrir era a esteira reprovar — foi o que aconteceu com o `basalto`, que
nasceu sem `SONAR_TOKEN` e sem `GITOPS_SSH_KEY` e só mostrou isso num job
vermelho depois de a esteira inteira rodar. `--conferir` responde antes.

**O critério é o caminho qualificado, não o nome do arquivo.** Procurar
`pipeline.yml` trouxe o `TranslateReader`, que tem um `pipeline.yml` próprio sem
relação nenhuma com isto; procurar `sonar.yml` trouxe o `homelab-gitops`, que só
usa o `helm-lint.yml`. Gravar um segredo onde ele não serve não quebra nada hoje
— cria mais um segredo para vazar e para lembrar de rotacionar.

O valor vai por **stdin**, nunca por argumento: `gh secret set --body "$TOKEN"`
põe o segredo no `argv`, e `argv` é legível por qualquer processo da máquina.

**Ele confere o valor na origem antes de gravar**, e imprime uma impressão
digital (tamanho + sha256 curto) que identifica sem revelar. Isso também nasceu
de erro medido: um token mal lido grava sem reclamar, e o sintoma aparece três
execuções depois num `403` do SonarCloud que manda "conferir o token" — custou
dois diagnósticos errados antes de chegar na causa. Agora:

```
Impressao digital: 27 caracteres, sha256 eb224bfb4350
Conferindo o valor na origem antes de gravar...
::error::HTTP 401 — o SonarCloud recusou este token. NADA foi gravado.
```

Em **Git Bash ou MSYS**, prefira `--arquivo` ou a variável de ambiente: o
`getpass` do Python não lê colagem de forma confiável nesses terminais.

---

## Pacote privado no `npm ci` e no `docker build`

Um frontend que dependa de pacote privado do próprio dono — `@dono/design-system`
no npm do GitHub, por exemplo — precisa de token em **três** pontos, e não em um:
o `npm ci` do job de qualidade, o `comando_testes` do Sonar (que instala de novo,
porque roda os testes numa máquina própria) e o `docker build`. Esquecer qualquer
um dá 401 num lugar diferente, em momentos diferentes.

O `pipeline.yml` recebe **um** secret opcional e o distribui pelos três:

```yaml
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
      GITOPS_SSH_KEY: ${{ secrets.GITOPS_SSH_KEY }}
      GH_PACKAGES_TOKEN: ${{ secrets.GH_PACKAGES_TOKEN }}
```

No repositório, o `.npmrc` **referencia** a variável em vez de trazer o token
escrito — é o que permite versionar o arquivo:

```
@dono:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${NODE_AUTH_TOKEN}
```

Nos jobs de node, o token entra como `NODE_AUTH_TOKEN`. No build da imagem ele
entra como **secret do BuildKit**, com id `npm_token`, e o Dockerfile o consome
assim:

```dockerfile
RUN --mount=type=secret,id=npm_token     NODE_AUTH_TOKEN="$(cat /run/secrets/npm_token)" npm ci --ignore-scripts
```

**Por que não `build-arg`.** `ARG` fica gravado no histórico da imagem e sai
inteiro num `docker history` — quem puxa a imagem do GHCR lê o token. O
`--mount=type=secret` monta o valor em tmpfs só durante aquele `RUN`, e não
entra em camada nenhuma: nem na imagem, nem no cache `type=gha`, que guarda
**camadas intermediárias** e por isso vazaria um `npm config set` feito no
estágio de build.

Repositório que não consome pacote privado não passa o secret, não declara o
`--mount`, e nada muda: secret não consumido pelo BuildKit não emite nem aviso.

### `liberar_pacotes.py` — os quatro pontos num comando

O token faz falta em **quatro** lugares, e não em três: os do CI mais a máquina
de quem escreve, onde o `npm install` também responde 401.

```bash
# o que falta, sem mudar nada. Sai 1 se faltar algo.
python bin/liberar_pacotes.py --conferir --pacote @dono/pacote --repo dono/repo

# conserta: pede o escopo que falta e grava o secret
python bin/liberar_pacotes.py --aplicar  --pacote @dono/pacote --repo dono/repo
```

Ele pede `read:packages` ao `gh` (fluxo interativo, herdando o terminal — sem
isso o código de uso único não apareceria), grava o `GH_PACKAGES_TOKEN` nos
repositórios por **STDIN**, e imprime a linha de `export` da máquina local.
Idempotente: rodar de novo não estraga nada.

**O `--pacote` é o que dá valor à conferência.** Ter `read:packages` escrito na
lista de escopos não é a mesma coisa que o registro aceitar o token — escopo
revogado do lado do dono, token de organização sem acesso concedido ao
repositório, ou nome de pacote errado dão os três a mesma aparência de "está
tudo certo" e o mesmo 401 no `npm ci`. A prova é um GET no registro pedindo o
pacote que o projeto instala de verdade.

Por padrão o token que vai para o CI é o do próprio `gh`, que **morre junto com
a autorização do gh** — `gh auth logout` ou revogar o app derruba o CI sem
ninguém ter mexido nele. Para um token que não depende disso, crie um PAT com
`read:packages` e só isso e passe `--de-arquivo`; é o mesmo formato que o
`semear_secret.py` espera em `--de-diretorio`.

## Versão automática

Ninguém escreve número de versão em lugar nenhum. A esteira lê os commits desde
a última tag `vX.Y.Z` alcançável e decide o salto pela mensagem, no padrão
[Conventional Commits](https://www.conventionalcommits.org/pt-br/):

| Commit | Salto | |
|---|---|---|
| `feat!:` ou rodapé `BREAKING CHANGE:` | major | 1.4.2 → 2.0.0 |
| `feat:` | minor | 1.4.2 → 1.5.0 |
| `fix:` `perf:` `refactor:` `docs:` `test:` `build:` `ci:` `chore:` `style:` `revert:` | patch | 1.4.2 → 1.4.3 |
| qualquer outra coisa | **reprova o run** | |

O maior salto vence. Sem tag nenhuma, a primeira versão é `versao_inicial`
(`1.0.0`), seja qual for o conteúdo. Não há regra especial para `0.x`: uma
quebra em `0.4.2` vira `1.0.0`.

### O que é automático, e o único bit que não é

A esteira calcula o número, cria a tag e a release com as notas dos commits,
aponta `:<versão>` para o **mesmo digest** que `sha-<commit>` e leva a versão ao
GitOps. O que ela **não** consegue é saber se uma mudança quebra contrato:
nenhuma ferramenta descobre isso para qualquer linguagem. Esse bit vem da
mensagem do commit, e de mais nenhum lugar.

Por isso o padrão é estrito. Um commit sem tipo que virasse `patch` em silêncio
seria uma quebra de contrato publicada como correção — e o próximo a descobrir
seria quem atualizou "só o patch". Medido antes de decidir: dos 179 commits nos
cinco repositórios deste homelab, 148 já seguiam o padrão; os 31 restantes eram
frases soltas, todas recentes.

### O que acontece num push na `main`

1. `versao` corre em paralelo com todo o resto — só precisa do git — e produz
   `1.4.0`, `v1.4.0`, o salto e as notas.
2. `publicar` escreve no GitOps `image.tag: sha-…` **e** `versao: 1.4.0`.
3. `lancar`, o último: alias `:1.4.0` para o digest da imagem, `git tag`,
   release. Só depois de todos os portões e depois do GitOps.

Três decisões dentro disso:

**A identidade no cluster continua sendo o sha.** A versão vai para o chart
(`APP_VERSION`, `service.version`, `app.kubernetes.io/version`), não para
`image.tag`. Se fosse a tag deployada, dois commits com a mesma versão seriam
invisíveis para o ArgoCD: mesma tag, nenhum diff, nenhum rollout.

**A tag nasce por último, e o run é idempotente.** `lancar` recalcula a versão e
compara com a que `versao` calculou no início; se entrou tag ou commit no meio,
reprova sem criar nada. Um run cancelado pelo `cancel-in-progress` não deixa tag,
e o próximo chega ao mesmo número sozinho.

**Só `GITHUB_TOKEN`.** Tag criada com ele não dispara `on: push: tags` — e não
precisa: tudo o que depende da versão acontece no mesmo run. A alternativa, um
PAT para "acordar" outro workflow, é uma credencial de conta inteira guardada em
secret. É o que o release-please pede, e o que este desenho evita.

### Reprova o que entrou agora, lista o que já estava

Com commit direto na `main`, o commit ruim já entrou quando o run reprova. Ele
não sai sem reescrever histórico. Então a reprovação vale para os commits
**deste push** (`github.event.before..sha`) ou deste pull request; os
anteriores aparecem nas notas como "Sem tipo" e não contam para o salto. Sem
isso, um único commit errado travaria a esteira até alguém fazer force-push na
`main`.

Em pull request nada é criado: o painel do run mostra o que a mudança vai virar
("1.4.0, minor, 2 feat, 1 fix") e reprova se algum commit do PR estiver fora do
padrão.

**Correção nos templates só chega por run novo, não por `gh run rerun`.**
Medido no primeiro dia: o rerun re-resolve as composite actions `@main` (a
trava nova do `preparar` apareceu na tentativa 2), mas mantém o workflow
reutilizável da resolução original — a tentativa 3 falhou no ponto que já
estava corrigido aqui. Um commit vazio no repositório da aplicação resolve.

**Dois bugs que o primeiro dia mediu, e que ficaram como trava.** O
`historico-completo` do `preparar` nunca funcionou (`&& 0 || 1`, e `0` é falso
na expressão do Actions): o Sonar media "New Code" em clone raso e a varredura
de segredo "no histórico inteiro" via um commit só — os dois verdes. Agora o
`preparar` reprova se pediu histórico e o clone veio raso. E o recálculo dentro
de `lancar` reprovava commits antigos que o `versao` já tinha julgado; hoje ele
recebe a faixa vazia, porque ali nada é novo.

### O hook, para não descobrir no CI

A mesma regra, na máquina, antes de o commit existir:

```bash
git config core.hooksPath /caminho/para/github-workflows/hooks
```

[`hooks/commit-msg`](hooks/commit-msg) chama `bin/versao.py conferir`. Um
segundo ali economiza um run inteiro e um commit que já entrou. `git commit
--no-verify` pula uma vez, de propósito. `core.hooksPath` troca o diretório de
hooks inteiro: se o repositório já usa outros (`.husky`, `pre-commit`), some o
arquivo lá em vez de apontar para cá.

### Inputs

| Input | Padrão | |
|---|---|---|
| `versionar` | `true` | desliga tudo isto |
| `commits_sem_tipo` | `reprovar` | `patch` (vira correção) ou `ignorar` (não conta) |
| `versao_inicial` | `1.0.0` | primeira tag |
| `tag_movel_major` | `false` | também move `v1`, `v2`… — para repositório de templates e actions |

Quem chama precisa conceder `contents: write` (tag e release) e `packages: write`
(alias da imagem).

### Por que um script próprio

| | Última release | O que decidiu |
|---|---|---|
| **release-please** | v5.0.0, 2026-04 | Modelo de biblioteca: abre um Release PR, alguém faz merge, aí nasce a tag. Aqui todo push na `main` já vai para o cluster — a versão ficaria atrás do deploy. E o Release PR aberto com `GITHUB_TOKEN` não recebe checks: com o `Portao` obrigatório, ele nunca merge sem PAT. |
| **semantic-release** | 25.0.9, 2026-08 | Funciona sem npm, mas instala meia dúzia de plugins pelo npm a cada run — contra o lock de 22 actions por SHA. |
| **git-cliff** | 2.14.1, 2026-09 | Binário único, mantido, bom. Medido no 2.13.1: commit sem tipo dá `v1.0.0 → v1.0.0` em silêncio, e `no_increment_regex` foi ignorado sem aviso (só existe a partir do 2.14.0; o PyPI ainda está no 2.13.1). É o plano B se a regra crescer. |
| **github-tag-action** | v6.2, 2024-03 | Último push em 2024-08. Morta. |

A regra cabe em [`bin/versao.py`](bin/versao.py), roda no terminal, e
[`bin/versao_teste.py`](bin/versao_teste.py) a prova nas duas cores em vinte
casos — inclusive `v1.10.0` acima de `v1.9.0` (ordenar como texto erra) e a tag
móvel `v1` ignorada.

### Helm é a exceção estrutural

`Chart.yaml` exige `version`. No [`helm-charts`](https://github.com/slipalison/helm-charts)
o campo é `0.0.0` e o CI carimba a versão calculada com `helm package
--version` — o ArgoCD lê o pacote no GHCR, não o arquivo no git. Nenhum número
escrito à mão, e nenhum commit de bot na `main`.

---

## Rodar a esteira na sua máquina antes do PR (`validar-local`)

Num repositório **privado**, todo minuto de runner sai da cota da conta, e o
GitHub cobra cada job em minuto cheio — um job de 5 s custa 1 min. Esta esteira
é pública, mas roda **na conta de quem a chama**. Em setembro de 2026 a cota Free
(2.000 min/mês) acabou no dia 23: cada push num PR aberto do `basalto` custava
~52 min, e 21% do que se gastou foi em runs que falharam — erro que dava para
ver antes do push.

[`bin/validar_local.py`](bin/validar_local.py) roda, na sua máquina, os
workflows que o PR vai rodar — **os próprios**, e não uma cópia dos passos:

```bash
cd meu-repositorio
python3 /caminho/para/github-workflows/bin/validar_local.py            # tudo o que roda em pull_request
python3 /caminho/para/github-workflows/bin/validar_local.py -W e2e.yml # um workflow
python3 /caminho/para/github-workflows/bin/validar_local.py --status   # o HEAD já passou?
```

- **O mesmo YAML.** Ele usa o [`act`](https://github.com/nektos/act) em modo
  host: cada `run:` executa aqui, com expressões, `if:`, matrizes, as actions de
  terceiro e os workflows reutilizáveis desta esteira. A esteira é a `main` de
  `origin` (o `@main` que o CI usa); `--esteira-local` usa a sua cópia, para
  validar uma mudança **aqui** antes do PR dela.
- **Fica de fora só o que é do GitHub**, e cada um vira uma action vazia:
  - `harden-runner` — em modo host ele instalaria um agente de rede na sua máquina;
  - `codeql-action` — manda SARIF para a aba Security;
  - `docker/login-action` — sem `GITHUB_TOKEN` aqui, e o PR não publica imagem;
  - o **Sonar** — a análise é do SonarCloud, e só o CI a mede;
  - a **publicação da imagem** — o job `imagem` publica `sha-<commit>` no GHCR
    até no PR; na cópia da esteira que ele monta, todo `push: true` vira
    `push: false`. A imagem é construída e varrida pelo Trivy do mesmo jeito.
- **Três diferenças do runner que ele cobre:** o `upload-artifact` depois da v4
  vira a v4.6.2 (a API nova não existe no servidor do `act`); o Python do
  sistema vai para o *tool cache* (o `setup-python` só tem binário para Ubuntu);
  e o servidor de cache escuta na `docker0`, porque o `cache-to: type=gha` do
  build roda dentro do contêiner do BuildKit.
- **O TruffleHog com teto de memória.** É a mesma action, no mesmo SHA, com
  `--memory=2g` no `docker run`: ela varre o histórico inteiro num contêiner, e
  numa máquina de 4 GB derrubava a sessão de quem chamou.
- **Protege a máquina.** Os passos rodam numa cópia do repositório, com HOME
  isolado em `~/.cache/validar-local/home` e sem token com escrita. `git@github.com:`
  vira HTTPS com a credencial do `gh`. Um `validar-local` por vez, e um job por
  vez, porque dividem Docker e portas. No fim, ele confere que a árvore não mudou.
- **Segredo nunca por argumento.** Vem do ambiente e dos arquivos
  `~/.config/<repositório>/*.env`. `GH_PACKAGES_TOKEN` é o token do `gh`.
- **`actionlint` só nos workflows que mudaram** em relação à `main`: um aviso
  antigo não trava ninguém, e um novo não passa.
- **Registro.** Tudo verde, com a árvore commitada, grava
  `.git/validar-local/<árvore>`. É o que diz, no PR, que aquela árvore passou.

Precisa de Docker, `gh` com login e Python 3.11+, mais o que o runner do GitHub
traz e os workflows usam sem instalar (`jq`, `zstd`, `unzip`, PyYAML no Python
do sistema) — ele confere antes de rodar e diz o que falta. As versões do
`act`, do `actionlint` e do `yq` estão fixadas no script, com sha256.

**Memória.** Numa máquina de 4 GB, o `cargo`, o Semgrep e o TruffleHog juntos
estouram a memória. Ele roda um job por vez, limita o `cargo` a dois processos e
se marca como a primeira vítima do OOM killer (`oom_score_adj=1000`), para que
seja a validação a morrer, e não a sessão de quem a chamou. Um `/tmp` em tmpfs
conta como memória.

## O que tem aqui

| Arquivo | O que faz |
|---|---|
| [`pipeline.yml`](.github/workflows/pipeline.yml) | Orquestrador. É o único que a aplicação precisa chamar. |
| [`qualidade.yml`](.github/workflows/qualidade.yml) | Lint, testes e cobertura — um job paralelo por componente. dotnet, python, node, go, rust. |
| [`seguranca.yml`](.github/workflows/seguranca.yml) | Gitleaks, TruffleHog, Semgrep, CodeQL, SCA (Trivy fs), IaC e SBOM, em paralelo. |
| [`sonar.yml`](.github/workflows/sonar.yml) | SonarQube/Cloud com Quality Gate e o guard anti-pulo. |
| [`build-push.yml`](.github/workflows/build-push.yml) | Imagem: constrói, varre, publica. Devolve `tag` e `digest`. |
| [`deploy.yml`](.github/workflows/deploy.yml) | Escreve a tag e a versão no GitOps. Não toca no cluster — quem aplica é o ArgoCD. |
| [`versao.yml`](.github/workflows/versao.yml) | Próxima versão pelos commits. Reprova commit fora do Conventional Commits. |
| [`lancar.yml`](.github/workflows/lancar.yml) | Alias `:<versão>` da imagem, tag, release. O último job, só na main. |
| [`helm-lint.yml`](.github/workflows/helm-lint.yml) | Renderiza o chart com os valores reais e recusa `:latest`. |
| [`ci.yml`](.github/workflows/ci.yml) | O CI **deste** repositório — que também se versiona. |

Composite actions: [`preparar`](.github/actions/preparar/action.yml),
[`relatar-sarif`](.github/actions/relatar-sarif/action.yml),
[`relatar-cobertura`](.github/actions/relatar-cobertura/action.yml),
[`versao`](.github/actions/versao/action.yml).

Scripts: [`resumo_sarif.py`](bin/resumo_sarif.py),
[`cobertura.py`](bin/cobertura.py), [`pinar_actions.py`](bin/pinar_actions.py),
[`semear_secret.py`](bin/semear_secret.py), [`versao.py`](bin/versao.py),
[`validar_local.py`](bin/validar_local.py).

Hook: [`hooks/commit-msg`](hooks/commit-msg), a mesma regra de `versao.py` antes
de o commit existir.

### Por que Python, e não Node ou Go

Já está no runner, roda sem passo de instalação, e o Actions aceita
`shell: python` nativamente. Node também está, mas Go exigiria um passo de build
antes do primeiro uso — custo fixo em todo job, para resolver o mesmo problema.

Os scripts rodam **fora do CI**, no terminal, com os arquivos na mão. Isso
não é conveniência: é o que permite provar que um portão reprova quando deve,
sem abrir um pull request só para ver a cor.

---

## Os componentes

Um componente é uma unidade que compila e testa sozinha. Cada um vira um job
paralelo.

```yaml
componentes: |
  [
    {"nome":"backend","linguagem":"dotnet","versao":"10.0.x",
     "projeto":"Onboarding.slnx","cobertura":80},
    {"nome":"frontend-client","linguagem":"node","versao":"24",
     "projeto":"frontend-client"}
  ]
```

| Campo | |
|---|---|
| `nome` | rótulo do job e do artefato. Sem ele a tela mostra `qualidade (Object)` em todos. |
| `linguagem` | `dotnet` \| `python` \| `node` \| `go` \| `rust` |
| `versao` | opcional |
| `caminho` | diretório de trabalho, padrão `.` |
| `projeto` | dotnet: `.sln`/`.csproj` · node: workspace · go: `./...` · rust: pacote do workspace (`--package`), vazio = `--workspace` |
| `cobertura` | piso em %; `0` (padrão) desliga |
| `scripts_de_instalacao` | node: `true` deixa o `npm ci` rodar `postinstall` e afins. O padrão é `--ignore-scripts`: script de instalação roda antes de qualquer teste, e é o vetor clássico de pacote comprometido. Ligue só para pacote nativo que precise compilar. |

`fail-fast: false` de propósito: o padrão mata os outros componentes quando um
falha e mostra só o primeiro erro — com três componentes, isso vira três rodadas
de CI para descobrir três problemas que dava para ver de uma vez.

### Rust

Entrou em 2026-09-15, com o `basalto`. Três decisões que valem registrar:

**Sem action de terceiro.** A imagem do runner já traz o toolchain stable com
`cargo` e `rustup` no PATH. Uma action só para instalar o que já existe seria
mais uma dependência fixada por SHA, com acesso ao token do run, para fazer
nada. `preparar` só acrescenta os componentes (`rustfmt`, `clippy`,
`llvm-tools-preview`) e, quando `versao` vem preenchida, instala e fixa aquele
toolchain.

**`cargo-llvm-cov` por binário pronto, em versão fixa.** Compilar a ferramenta
custa perto de dois minutos e ela não é o que está sendo testado. A versão é
fixa e não `latest/download` — mesmo argumento dos SHAs em `actions.lock.json`:
`latest` é ponteiro móvel, e quem publica decide o que roda aqui dentro.

**`cargo llvm-cov` já roda os testes.** Não há `cargo test` antes dele: seria a
mesma suíte duas vezes. A cobertura sai em LCOV, que é o que o portão desta
esteira lê e o que o Sonar importa.

Dois caches, em lugares diferentes e por um motivo: `~/.cargo/registry` e
`~/.cargo/git` são globais e ficam em `preparar`; o `target/` depende do
`caminho` do componente, e um passo `uses:` ignora
`defaults.run.working-directory` — então ele é cacheado em `qualidade.yml`, onde
o caminho é conhecido.

No Sonar, as propriedades são `sonar.rust.lcov.reportPaths` (ou
`sonar.rust.cobertura.reportPaths`) e `sonar.rust.cargo.manifestPaths`. O
analisador roda o Clippy sozinho — `sonar.rust.clippy.enabled` vem ligado.

---

## Pré-requisitos fora daqui

1. **`apps/<nome>/values.yaml` no repositório de GitOps.** O deploy falha de
   propósito se não existir: quem define o que um app é não é o pipeline.
2. **Credencial de escrita no GitOps** — `GITOPS_SSH_KEY` (chave de deploy
   criada *no* repositório de GitOps, preferida: alcança aquele repositório e
   mais nada) ou `GITOPS_TOKEN` (fine-grained, `Contents: RW`).
3. **`permissions` declarado no job que chama** — `contents: write` para a tag e
   a release, `packages: write` para a imagem e o alias, `security-events:
   write` para o SARIF. Sem isso o run morre em `startup_failure` com
   *"requesting 'packages: write', but is only allowed 'packages: read'"*, que
   **não aparece no log de passo nenhum**.
4. **`SONAR_TOKEN`** no repositório, para a análise.
5. **Imagem acessível ao cluster.** Pacote privado no GHCR sem `imagePullSecret`
   deixa o pod em `ImagePullBackOff` dizendo que não encontrou a imagem — o que
   não parece um problema de credencial.
