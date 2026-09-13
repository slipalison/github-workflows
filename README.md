# github-workflows

Workflows reutilizáveis de CI/CD. Cada repositório de aplicação chama estes em
vez de copiar cem linhas de YAML que depois divergem.

O desenho tem um princípio, e vale entendê-lo antes de ler o resto: **o CI não
fala com o cluster.** Ele constrói a imagem e escreve um commit num repositório
de GitOps. O cluster puxa. Não existe `kubeconfig` guardado no GitHub, e o que
está rodando é sempre um arquivo que alguém pode ler, culpar e reverter.

---

## O caminho completo, do push ao cluster

```
git push (main)
   │
   ▼
ci.yml no repositório do app
   ├─ testes ........................ dotnet.yml
   ├─ imagem ........................ build-push.yml
   │    ├─ tag = sha-<7 primeiros do commit>
   │    ├─ build com cache, push no GHCR
   │    └─ Trivy: CRITICAL derruba o build, HIGH fica no log
   └─ publicar (só na main) ......... deploy.yml
        ├─ clona o repositório de GitOps com o GITOPS_TOKEN
        ├─ yq -i '.image.tag = "sha-…"' em apps/<app>/values.yaml
        └─ commit + push        ← o CI acaba aqui
   │
   ▼
ArgoCD vê o commit e sincroniza
   │
   ▼
Argo Rollouts: canary → análise sobre as métricas → 100 %, ou aborta
```

Pull request roda testes e imagem e **para aí**. Só a branch principal chega ao
cluster.

---

## Os workflows

| Workflow | O que faz |
|---|---|
| `build-push.yml` | Imagem com cache, tag `sha-<7>`, push no GHCR e varredura Trivy. Devolve a tag. |
| `deploy.yml` | Escreve a tag em `apps/<app>/values.yaml` do repositório de GitOps e commita. Não toca no cluster. |
| `dotnet.yml` | Restore, build e testes com cobertura. |
| `helm-lint.yml` | Renderiza o chart com os valores reais e recusa `:latest`. |
| `exemplo-ci.yml` | Modelo para copiar no repositório de um app. |

### `build-push.yml`

| | |
|---|---|
| **Entradas** | `image` (obrigatória, ex.: `ghcr.io/usuario/meu-app`), `context` (`.`), `dockerfile` (`Dockerfile`), `platforms` (`linux/amd64`) |
| **Saída** | `tag` — `sha-<7>`, o que o `deploy.yml` recebe |
| **Permissões** | `packages: write` (usa o `GITHUB_TOKEN` do próprio repositório) |

A tag é o commit, não `latest`. É reproduzível, diz de onde veio, e políticas de
admissão em cluster costumam recusar `latest` — com razão: `latest` é um nome
que muda de significado sozinho.

O Trivy derruba o build só em **CRITICAL**. `HIGH` aparece no log e vira
trabalho planejado. Derrubar tudo ensina o time a ignorar o relatório.
Exceções em `.trivyignore`, no repositório do app.

### `deploy.yml`

| | |
|---|---|
| **Entradas** | `app` (= diretório em `apps/` do GitOps), `tag`, `gitops_repo` (padrão `slipalison/homelab-gitops`), `via_pr` (padrão `false`) |
| **Segredo** | `GITOPS_TOKEN` — **obrigatório** |

Usa `yq`, não `sed`: `values.yaml` é YAML, e `sed` em YAML funciona até o dia em
que a indentação muda.

Se `apps/<app>/values.yaml` não existir, ele **falha com mensagem clara** em vez
de criar o arquivo. É de propósito: quem define o que uma aplicação é — porta,
réplicas, limites, hostname — não é o pipeline.

`via_pr: true` abre um pull request em vez de commitar direto. É o caminho para
ambiente que exige aprovação humana antes do deploy.

### `helm-lint.yml`

| | |
|---|---|
| **Entradas** | `values` (obrigatória), `chart` (padrão `oci://ghcr.io/slipalison/charts/app`), `chart_version` |

**Roda no repositório que guarda o `values.yaml`** — normalmente o de GitOps, não
o do app.

`helm lint` sozinho não executa o template; um erro que só aparece com os
valores de verdade passa por ele sem uma palavra. Por isso aqui é `helm
template` com o values real, e uma verificação explícita de `:latest` no
resultado.

**Mantenha `chart_version` igual à versão que o cluster usa.** Validar contra
outra versão é pior do que não validar: passa no CI e renderiza diferente lá.

---

## Ligar uma aplicação nova

### 1. No repositório de GitOps: criar o diretório do app

```yaml
# apps/meu-app/values.yaml
name: meu-app
owner: fulano

image:
  repository: ghcr.io/usuario/meu-app
  tag: sha-0000000      # o CI reescreve esta linha a cada deploy

port: 8080
replicas: 2

resources:
  requests: { cpu: 10m, memory: 64Mi }
  limits:   { memory: 256Mi }
```

Este é o contrato do chart [`app`](https://github.com/slipalison/helm-charts),
que monta Rollout com canary, Service, VirtualService, NetworkPolicy e o resto.
Com um `ApplicationSet` varrendo `apps/*`, o diretório basta — não se escreve
`Application` à mão.

### 2. No repositório do app: o `ci.yml`

```yaml
# .github/workflows/ci.yml
on:
  push:
    branches: [main]
  pull_request:

jobs:
  testes:
    uses: slipalison/github-workflows/.github/workflows/dotnet.yml@main

  imagem:
    needs: testes
    uses: slipalison/github-workflows/.github/workflows/build-push.yml@main
    with:
      image: ghcr.io/usuario/meu-app

  publicar:
    needs: imagem
    if: github.ref == 'refs/heads/main'
    uses: slipalison/github-workflows/.github/workflows/deploy.yml@main
    with:
      app: meu-app
      tag: ${{ needs.imagem.outputs.tag }}
    secrets:
      GITOPS_TOKEN: ${{ secrets.GITOPS_TOKEN }}
```

### 3. O segredo `GITOPS_TOKEN`

```bash
gh secret set GITOPS_TOKEN -R usuario/meu-app
```

Token **fine-grained**, com `Contents: Read and write` **apenas** no repositório
de GitOps. Não é o `GITHUB_TOKEN` automático — aquele não alcança outro
repositório. O que não se faz é dar escopo de conta inteira a um workflow.

### 4. Deixar a imagem acessível ao cluster

O cluster puxa a imagem. Se o pacote no GHCR estiver **privado** e não houver
`imagePullSecret` configurado, o pod fica em `ImagePullBackOff` — e a mensagem
não diz "falta credencial", diz que não encontrou.

Ou o pacote é público (*Package settings → Change visibility*), ou o chart
precisa de `imagePullSecrets` e um `dockerconfigjson` no namespace do app.

---

## O que só se descobre fazendo

Quatro coisas que não estão em nenhum tutorial e custam uma noite cada.

**1. Canary sem tráfego não conclui.** Se a análise do Rollouts consulta a taxa
de sucesso num Prometheus, sem requisição não há série — e sem série a análise
**erra**, não passa. Numa aplicação de pouco acesso, um deploy legítimo pode
abortar sem nada estar errado. Ou se garante tráfego durante a janela, ou se
afrouxam `count` e `interval` naquele app.

**2. Um rollout abortado não volta com um commit novo.** Se a imagem for a
mesma, não há revisão nova a criar e o GitOps não tem o que fazer:

```bash
kubectl argo rollouts retry rollout meu-app -n meu-app
```

**3. O primeiro deploy nunca é canary.** Sem versão anterior não há para onde
dividir tráfego: o Rollout sobe direto. O canary vale do segundo deploy em
diante.

**4. Se o hostname vem de um CNAME curinga, a aplicação nasce pública** no
instante em que o `VirtualService` sincroniza. Publicar primeiro e proteger
depois é uma janela aberta, por menor que pareça.

---

## Quando o deploy não chega ao cluster

Na ordem, do mais comum ao mais raro:

```bash
# 1. o CI escreveu a tag?
git -C gitops log --oneline -3 -- apps/meu-app/values.yaml

# 2. o ArgoCD viu o commit?
kubectl -n argocd get app meu-app \
  -o custom-columns=SYNC:.status.sync.status,SAUDE:.status.health.status,REV:.status.sync.revision

# 3. o rollout está andando, parado ou abortado?
kubectl argo rollouts get rollout meu-app -n meu-app

# 4. a análise reprovou?
kubectl -n meu-app get analysisrun

# 5. o pod subiu?
kubectl -n meu-app get pods
```

`ImagePullBackOff` no passo 5 quase sempre é o pacote privado no registro.

---

## O que estes workflows deliberadamente não fazem

- **Não aplicam no cluster.** Sem `kubeconfig` no GitHub, sem `kubectl apply`
  em workflow. O preço é que o CI não sabe se o deploy deu certo; quem sabe é o
  ArgoCD, e é lá que se olha.
- **Não promovem entre ambientes.** Com mais de um cluster, o caminho é
  `via_pr: true` — a mesma esteira abrindo um pull request em vez de commitar.
- **Não fazem rollback por pipeline.** Reverter é `git revert` no GitOps. O
  rollback automático do canary é outra coisa: acontece em segundos, sem commit
  nenhum, e é o que segura um deploy ruim antes de virar problema.
