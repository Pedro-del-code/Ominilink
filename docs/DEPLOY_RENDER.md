# Implantação no Render

*Regras do Render conferidas em **18/10/2026** na documentação oficial (links ao final). Planos e limites mudam: confirme
no painel antes de contratar. Esta implantação **não foi executada** durante a construção do projeto (sem rede); o que foi
validado localmente está listado na seção 8 — e `examples/smoke_remote.py` verifica o serviço real em minutos.*

## 1. Escolha do plano e o que ela implica

| Item | Plano gratuito (`plan: free`) | Consequência para o OmniLink |
|---|---|---|
| Inatividade | A instância **dorme após 15 min sem tráfego de entrada** e acorda na próxima requisição (≈ 1 min) | Primeira chamada após ociosidade é lenta; clientes têm reconexão com recuo (CLI: 8 tentativas, até 15 s ≈ 1 min) |
| Horas | 750 horas de instância gratuitas por mês, por *workspace* | Um serviço 24/7 consome ≈ 744 h |
| Sistema de arquivos | **Efêmero** (perdido em *deploy*, reinício e suspensão); sem disco persistente | Nada essencial é gravado em arquivos; estado vai para PostgreSQL |
| Escala | Uma única instância | Compatível: o coordenador é de instância única por projeto |
| Recursos | ≈ 0,1 CPU e 512 MB de RAM | Limites de imagem conservadores (2 MB, 8 megapixels) |
| PostgreSQL gratuito | 1 por *workspace*, 1 GB, **expira 30 dias após a criação** (+14 dias de tolerância, depois é apagado), sem backups | Dispositivos, autorizações e fila **desaparecem** quando expira; use plano pago ou outro PostgreSQL para uso contínuo |
| Reinícios | O Render pode reiniciar serviços gratuitos a qualquer momento | O estado precisa estar no banco; conexões abertas caem e os clientes reconectam |

**Sem `DATABASE_URL` o estado fica na memória**: qualquer suspensão por inatividade apaga todos os dispositivos e tokens.
Por isso o `render.yaml` já cria e liga um PostgreSQL. Recomendação: gratuito para **testar**; planos pagos (serviço e
banco) para **uso contínuo**.

> Como o fluxo SSE é renovado a cada 120 s (uma nova requisição de entrada), um receptor conectado tende a manter a
> instância acordada. Isto é uma **inferência** da regra de 15 min, **não verificada** em produção.

## 2. Publicar (passo a passo)

### 2.1 Conectar um repositório Git

```bash
cd omnilink
git init
git add .
git commit -m "OmniLink v0.1"
git branch -M main
git remote add origin git@github.com:SEU_USUARIO/omnilink.git     # GitHub, GitLab ou Bitbucket
git push -u origin main
```

O `.gitignore` já exclui `.env`, bancos locais, credenciais e `inbox/`. **Nunca** faça *commit* da chave de registro.

### 2.2 Criar os recursos pelo *Blueprint* (recomendado)

1. No painel do Render: **New → Blueprint**, conecte sua conta Git e escolha o repositório e a ramificação `main`.
2. O Render lê o `render.yaml` e mostra os recursos: o *web service* `omnilink-coordinator` e o banco `omnilink-db`.
3. Confirme (**Apply**). O Render instala (`pip install -r requirements.txt`), inicia com `gunicorn` e consulta `/health`.
4. Abra o serviço → **Environment** e leia o valor gerado de `OMNILINK_ENROLLMENT_KEY` (é o segredo de registro).
5. Verifique: `curl https://SEU-SERVICO.onrender.com/health` deve responder `{"status":"ok", … "store":"postgresql", "persistent":true}`.
6. Teste o fluxo completo (cria e revoga dispositivos temporários):

```bash
export OMNILINK_ENROLLMENT_KEY="valor lido no painel"
python examples/smoke_remote.py --url https://SEU-SERVICO.onrender.com
```

### 2.3 Alternativa manual (sem Blueprint)

*New → Web Service* com: **Runtime** Python; **Build** `pip install -r requirements.txt`; **Start**
`gunicorn wsgi:app --worker-class gthread --workers 1 --threads 16 --timeout 120 --graceful-timeout 30 --bind 0.0.0.0:$PORT --access-logfile -`;
**Health Check Path** `/health`; variáveis `OMNILINK_ENV=production`, `OMNILINK_ENROLLMENT_KEY=<segredo longo>` e
`DATABASE_URL` com a **Internal Database URL** de um PostgreSQL criado na mesma região. A versão do Python vem do arquivo
`.python-version` (`3.12`).

## 3. O que o `render.yaml` configura

| Chave | Valor | Motivo |
|---|---|---|
| `runtime: python`, `buildCommand` | `pip install -r requirements.txt` | Instalação padrão |
| `startCommand` | `gunicorn wsgi:app --worker-class gthread --workers 1 --threads 16 … --bind 0.0.0.0:$PORT` | O Render injeta `PORT` (padrão 10000) e exige escutar em `0.0.0.0`. **1 worker**: sessões e sinais em memória; **threads**: cada SSE/long-poll ocupa uma |
| `healthCheckPath` | `/health` | 200 quando o serviço e o banco respondem; 503 se o banco falhar |
| `OMNILINK_ENV=production` | — | Liga HTTPS obrigatório, logs JSON e exige chave forte |
| `OMNILINK_ENROLLMENT_KEY` | `generateValue: true` | O Render gera um valor aleatório; o segredo **não** está no repositório |
| `DATABASE_URL` | `fromDatabase … connectionString` | URL interna do PostgreSQL (rede privada, mesma região) |
| Banco `omnilink-db` | `plan: free` | Troque para um plano pago para uso contínuo |

HTTPS: o Render redireciona HTTP→HTTPS, gerencia os certificados e termina o TLS no *proxy*; o aplicativo recebe HTTP com
`X-Forwarded-Proto: https` (informação da equipe do Render em fórum oficial) e, com `OMNILINK_TRUST_PROXY` (ligado quando
`RENDER=true`), usa o cabeçalho de **um** salto de proxy. `/health` é isento da checagem HTTPS porque o *health check*
chega por HTTP interno.

## 4. Variáveis de ambiente

A lista completa, com padrões, está em [ARQUITETURA_E_PROTOCOLO.md](ARQUITETURA_E_PROTOCOLO.md#11-configuração-variáveis-de-ambiente).
As que normalmente se ajustam: `OMNILINK_ENROLLMENT_KEY` (obrigatória), `DATABASE_URL`, `OMNILINK_TOKEN_TTL_DAYS`,
`OMNILINK_QUEUE_TTL_SECONDS`, `OMNILINK_MAX_IMAGE_BYTES`, `OMNILINK_RATE_LIMIT_PER_MINUTE`, `OMNILINK_LOG_LEVEL`.
Segredos ficam **só** no painel (variáveis de ambiente) — nunca no repositório.

## 5. Logs e diagnóstico

* **Painel:** serviço → **Logs** (acompanhamento ao vivo e filtro). Há também o *Render CLI* (`render logs`; veja `render logs --help`).
* Em produção os logs são **JSON, uma linha por evento**, sem credenciais nem conteúdo (testado). Exemplos:
  `{"ts":…,"level":"INFO","logger":"omnilink.audit","msg":"event=message.accepted message=msg_… sender=dev_… receiver=dev_… kind=text …"}`
* Trilha de auditoria recente (sem segredos): `GET /v1/admin/events?limit=100` com o cabeçalho `X-Enrollment-Key`.
* Estado do serviço: `GET /health` (`store`, `persistent`).

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| *Deploy* falha no `pip install` | Versão do Python incompatível | Confirme `.python-version` = `3.12` (o `PYTHON_VERSION` do painel, se existir, tem precedência e exige versão completa) |
| "No open ports detected" / *timeout* | O processo não escuta em `0.0.0.0:$PORT` | Use o `startCommand` do `render.yaml` |
| *Deploy* reverte após o *health check* | `/health` ≠ 200: falta `OMNILINK_ENROLLMENT_KEY` (erro de configuração no log) ou o banco não responde | Veja os logs de inicialização; confira `DATABASE_URL` |
| 426 `TLS_REQUIRED` | Requisição chegou sem `X-Forwarded-Proto: https` | Use a URL `https://…`; mantenha `OMNILINK_TRUST_PROXY` ligado; se houver outro proxy na frente, revise a cadeia |
| 401 `AUTH_EXPIRED` | Token com mais de `OMNILINK_TOKEN_TTL_DAYS` | Registre de novo ou use `rotate-token` antes de vencer |
| Todos os dispositivos "sumiram" | Sem `DATABASE_URL`, ou PostgreSQL gratuito expirou | Ligue um banco; para continuidade use plano pago |
| 429 `RATE_LIMITED` no registro | Muitos registros/falhas do mesmo IP (ou *proxy* agrupando clientes) | Ajuste `OMNILINK_IP_RATE_LIMIT_PER_MINUTE` |
| Fluxos caem a cada ≈ 2 min | Projetado (`OMNILINK_SSE_MAX_SECONDS`); o cliente reconecta | Nenhuma ação |
| Primeira chamada demora ≈ 1 min | Instância acordando (plano gratuito) | Aguarde; os clientes tentam de novo com recuo |
| Erros de conexão com o banco | URL externa em vez da interna, ou outra região | Use `fromDatabase … connectionString` (interna) na mesma região |

## 6. Limites de conexões e recursos a considerar

* Cada receptor conectado ocupa **1 thread** (SSE ou long-poll). Com `--threads 16`, ≈ 16 receptores simultâneos; aumente
  `--threads` (consome memória) ou migre para um servidor assíncrono se precisar de mais.
* Conexões abertas **caem** em cada *deploy*/reinício (o roteamento sem interrupção só vale para novas conexões); os
  clientes reconectam e recebem o que ficou na fila.
* O coordenador roda em **uma instância**; não ative múltiplos *workers* nem escala horizontal sem antes trocar sinais/"online" por Redis ou
  `LISTEN/NOTIFY`.
* Tempo máximo de requisição: segundo a equipe do Render em fórum, 100 minutos; o OmniLink usa esperas de ≤ 25 s e fluxos de 120 s.

## 7. Rotação de segredos e manutenção

* Trocar `OMNILINK_ENROLLMENT_KEY` (Environment → editar → *redeploy*) impede **novos** registros com a chave antiga;
  tokens de dispositivos existentes continuam válidos até serem revogados ou expirarem.
* Revogar um dispositivo: `DELETE /v1/devices/me` (ele mesmo) ou `DELETE /v1/admin/devices/<id>` com a chave de registro.
* Um dispositivo comprometido: revogue-o; suas autorizações e fila são apagadas.

## 8. O que foi (e o que não foi) validado

* **Validado localmente:** o app em modo produção (`OMNILINK_ENV=production`, `RENDER=true`) com HTTPS atrás de proxy simulado
  por cabeçalhos, logs JSON sem segredos, falha rápida sem chave, `/health`, e `render.yaml` como YAML válido.
* **Não validado aqui:** `gunicorn` (indisponível), PostgreSQL real, o *Blueprint* no Render, TLS/proxy reais e o
  comportamento de suspensão do plano gratuito. Execute `examples/smoke_remote.py` após o primeiro *deploy*.

## Fontes consultadas (oficiais, exceto onde indicado)

* Planos gratuitos, suspensão, sistema de arquivos efêmero, PostgreSQL gratuito: <https://render.com/docs/free>
* Esquema do `render.yaml` (`runtime`, `plan`, `healthCheckPath`, `generateValue`, `fromDatabase`, `databases`): <https://render.com/docs/blueprint-spec>
* `PORT` (padrão 10000) e variáveis padrão: <https://render.com/docs/environment-variables> · escuta em `0.0.0.0`: <https://docs.render.com/web-services>
* Versão do Python (`.python-version`, `PYTHON_VERSION`): <https://render.com/docs/python-version>
* HTTPS e certificados: <https://docs.render.com/tls>
* *Health checks*/implantação sem interrupção: <https://render.com/articles/how-render-handles-zero-downtime-deploys>
* Fórum da comunidade (respostas da equipe; **não** é documentação oficial): `X-Forwarded-Proto` <https://community.render.com/t/proxy-https-settings/19249> · tempo máximo de requisição <https://community.render.com/t/need-to-increase-requests-timeout-of-5-minutes/4001>
