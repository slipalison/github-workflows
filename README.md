# github-workflows

Esteira de CI/CD reutilizável. Um repositório de aplicação chama **um** workflow
e recebe lint, testes, cobertura com piso, imagem com portão de vulnerabilidade,
sete varreduras de segurança, Sonar com Quality Gate e o commit no GitOps — com
tudo o que pode ser paralelo rodando em paralelo.

```yaml
jobs:
  esteira:
    permissions: { contents: read, packages: write, security-events: write, actions: read }
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
repositório poliglota .NET + dois frontends, e repositório privado.

---

## O desenho

```
        ┌─ qualidade  (N componentes, N jobs paralelos)
        ├─ imagem     (build → varre → publica)
push ───┼─ sonar      (análise + Quality Gate)          ─── portão ─── publicar
        └─ seguranca  (7 varreduras paralelas)                         (só na main)
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

---

## O que tem aqui

| Arquivo | O que faz |
|---|---|
| [`pipeline.yml`](.github/workflows/pipeline.yml) | Orquestrador. É o único que a aplicação precisa chamar. |
| [`qualidade.yml`](.github/workflows/qualidade.yml) | Lint, testes e cobertura — um job paralelo por componente. dotnet, python, node, go. |
| [`seguranca.yml`](.github/workflows/seguranca.yml) | Gitleaks, TruffleHog, Semgrep, CodeQL, SCA (Trivy fs), IaC e SBOM, em paralelo. |
| [`sonar.yml`](.github/workflows/sonar.yml) | SonarQube/Cloud com Quality Gate e o guard anti-pulo. |
| [`build-push.yml`](.github/workflows/build-push.yml) | Imagem: constrói, varre, publica. Devolve `tag` e `digest`. |
| [`deploy.yml`](.github/workflows/deploy.yml) | Escreve a tag no GitOps. Não toca no cluster — quem aplica é o ArgoCD. |
| [`helm-lint.yml`](.github/workflows/helm-lint.yml) | Renderiza o chart com os valores reais e recusa `:latest`. |
| [`ci.yml`](.github/workflows/ci.yml) | O CI **deste** repositório. |

Composite actions: [`preparar`](.github/actions/preparar/action.yml),
[`relatar-sarif`](.github/actions/relatar-sarif/action.yml),
[`relatar-cobertura`](.github/actions/relatar-cobertura/action.yml).

Scripts: [`resumo_sarif.py`](bin/resumo_sarif.py),
[`cobertura.py`](bin/cobertura.py), [`pinar_actions.py`](bin/pinar_actions.py).

### Por que Python, e não Node ou Go

Já está no runner, roda sem passo de instalação, e o Actions aceita
`shell: python` nativamente. Node também está, mas Go exigiria um passo de build
antes do primeiro uso — custo fixo em todo job, para resolver o mesmo problema.

Os três scripts rodam **fora do CI**, no terminal, com os arquivos na mão. Isso
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
| `linguagem` | `dotnet` \| `python` \| `node` \| `go` |
| `versao` | opcional |
| `caminho` | diretório de trabalho, padrão `.` |
| `projeto` | dotnet: `.sln`/`.csproj` · node: workspace · go: `./...` |
| `cobertura` | piso em %; `0` (padrão) desliga |

`fail-fast: false` de propósito: o padrão mata os outros componentes quando um
falha e mostra só o primeiro erro — com três componentes, isso vira três rodadas
de CI para descobrir três problemas que dava para ver de uma vez.

---

## Pré-requisitos fora daqui

1. **`apps/<nome>/values.yaml` no repositório de GitOps.** O deploy falha de
   propósito se não existir: quem define o que um app é não é o pipeline.
2. **Credencial de escrita no GitOps** — `GITOPS_SSH_KEY` (chave de deploy
   criada *no* repositório de GitOps, preferida: alcança aquele repositório e
   mais nada) ou `GITOPS_TOKEN` (fine-grained, `Contents: RW`).
3. **`permissions` declarado no job que chama** — `packages: write` para a
   imagem, `security-events: write` para o SARIF. Sem isso o run morre em
   `startup_failure` com *"requesting 'packages: write', but is only allowed
   'packages: read'"*, que **não aparece no log de passo nenhum**.
4. **`SONAR_TOKEN`** no repositório, para a análise.
5. **Imagem acessível ao cluster.** Pacote privado no GHCR sem `imagePullSecret`
   deixa o pod em `ImagePullBackOff` dizendo que não encontrou a imagem — o que
   não parece um problema de credencial.
