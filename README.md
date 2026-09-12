# github-workflows

Workflows reutilizaveis do homelab. Cada repositorio de aplicacao chama estes
em vez de copiar cem linhas de YAML que depois divergem.

| Workflow | O que faz |
|---|---|
| `build-push.yml` | Imagem com cache, tag `sha-<7>`, push no GHCR e varredura Trivy. Devolve a tag. |
| `deploy.yml` | Escreve a tag em `apps/<app>/values.yaml` do `homelab-gitops` e commita. Nao toca no cluster. |
| `dotnet.yml` | Restore, build e testes com cobertura. |
| `helm-lint.yml` | Renderiza o chart com os valores reais e recusa `:latest`. |
| `exemplo-ci.yml` | Modelo para copiar no repositorio de um app. |

## CI de um app novo

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
      image: ghcr.io/slipalison/meu-app

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

Falta uma coisa no repositorio do app: o secret `GITOPS_TOKEN`, um token
fine-grained do GitHub com `Contents: Read and write` **apenas** no
`homelab-gitops`. Sem ele o deploy nao tem onde escrever.

## Por que o deploy nao aplica no cluster

Porque entao o estado do cluster seria "o que o ultimo pipeline fez", que
ninguem consegue ler depois. Escrevendo um commit, o estado e um arquivo: da
para ver o diff, culpar, reverter, e o ArgoCD reconcilia sozinho se alguem
mexer no cluster a mao.
