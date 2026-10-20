# Segurança do OmniLink v0.1

**Uso permitido:** somente dispositivos, redes e serviços **próprios ou expressamente autorizados**. O projeto **não**
implementa — e não deve ser estendido para — invasão de redes, exploração de vulnerabilidades, evasão de autenticação,
execução remota não autorizada ou qualquer forma de contornar as proteções de um dispositivo. O objetivo é encontrar canais
**legítimos e compatíveis**; se um aparelho não oferece um, o resultado é um diagnóstico, não uma tentativa de forçá-lo.

## 1. Modelo de confiança

* **Um domínio de confiança por coordenador:** quem possui a *chave de registro* pode registrar dispositivos e revogá-los.
* **Dispositivos** se autenticam com *token* próprio (256 bits). **Receptores** decidem, por tipo de conteúdo, quem pode lhes enviar.
* **O coordenador é um relé de confiança:** ele vê o conteúdo (sem criptografia ponta a ponta nesta versão).
* **O receptor não confia no relé:** revalida destinatário, tipo, tamanho, imagem e HTML antes de apresentar.

## 2. Ameaças, controles e testes

| Ameaça | Controle implementado | Onde é testado | Risco residual |
|---|---|---|---|
| Registro por terceiros | Chave de registro comparada em tempo constante; limite por IP; auditoria | `test_api` (Registration…), `test_security` | A chave é um segredo compartilhado e também administra revogações |
| Adivinhar/roubar token | 256 bits; **só o hash SHA-256** é guardado; comparação constante; expiração (90 dias); rotação; revogação; bloqueio do IP após falhas | `test_security`, `test_coordinator`, `test_api` (Authentication…) | Token em claro fica no arquivo de credenciais do cliente (0600) |
| Envio não autorizado | Autorização **por tipo**, concedida pelo receptor; pedido sem conteúdo; negação persistente; pedidos pendentes limitados (20); revogação imediata | `test_coordinator` (Authorization…), `test_api` | O receptor pode aprovar tudo (`--auto-accept-all`, só demonstração, avisa em voz alta) |
| XSS por HTML recebido | Sanitização por lista de permissão no servidor **e** no receptor Python; `iframe sandbox=""` + CSP herdada no navegador | `test_content` (22 vetores + verificação estrutural), `test_browser_receiver` (HTML sem sanitizar, em Chromium) | Sanitizador próprio (biblioteca padrão) |
| Imagens maliciosas | Bytes mágicos; decodificação completa; limite de pixels e bytes; dados anexados (PNG/GIF/WebP); `<img>` só com `data:` | `test_content`, `test_api` (Hostile images) | JPEG pode ter dados anexados legítimos; EXIF não é removido |
| Injeção de comandos de terminal | Caracteres de controle (ESC) removidos no ingresso e na saída do console | `test_client_resilience` | — |
| Injeção SQL | Consultas parametrizadas; chave/valor genérico | `test_store` (payload `'; DROP TABLE`) | — |
| Travessia de diretórios | Arquivos gravados com nome derivado do `id`, nunca do remetente; `clean_filename` | `test_client_resilience` (Presenter) | — |
| Negação de serviço | Limites de corpo (413), pixels, fila (100), taxa por dispositivo e por IP (429), TTL, bomba de descompressão em codecs | `test_api` (Limits…), `test_content`, `test_codec` | Limitadores em memória, por processo; threads por fluxo |
| Interceptação | HTTPS obrigatório em produção (426) + HSTS; confiança em `X-Forwarded-*` somente se configurada | `test_api` (TransportSecurity…), `test_production_profile` | Confidencialidade só até o coordenador (sem E2E) |
| Vazamento em logs | Redação de `Bearer`, tokens e chaves; auditoria sem conteúdo; logs JSON | `test_security`, `test_api` (Audit…), `test_production_profile` | — |
| CSRF / CSWSH | Sem cookies (cabeçalho `Authorization`); sem CORS; sem WebSocket; página com CSP e `frame-ancestors 'none'` | `test_api` (headers), `test_browser_receiver` | — |
| Repetição/duplicação | `id` único, deduplicação no receptor, `client_id` (idempotência) no emissor | `test_coordinator`, `test_e2e_real_sockets` | — |
| Sessões após revogação | Evento SSE `revoked` (≤ 1 s) e encerramento; long-poll falha na chamada seguinte | `test_api` (Stream…), `test_e2e_real_sockets` | — |
| Descoberta UDP forjada | Respostas validadas (nonce, tipo, URL `http(s)`, nome); sem credenciais; anti-amplificação (sonda ≥ 256 B, resposta ≤ 256 B); limite por origem | `test_discovery` | Sem autenticação: **nunca** envie credenciais a um endereço apenas descoberto |
| Segredos no repositório | `.gitignore`; `.env.example` sem valores; `generateValue` no Render | revisão manual | — |

## 3. Boas práticas para operar

1. Use HTTPS sempre fora de `localhost` (no Render é automático). Em LAN com HTTP puro, trate a rede como hostil.
2. Gere a chave de registro com ≥ 16 caracteres aleatórios; não a coloque em linha de comando compartilhada (use a variável de ambiente).
3. Prefira aprovar emissores **um a um**; revise `grant list` periodicamente; revogue o que não usa.
4. Revogue dispositivos perdidos (`DELETE /v1/admin/devices/<id>`).
5. Mantenha as dependências atualizadas; fixe versões ao distribuir.
6. Não exponha o servidor de desenvolvimento (`python -m app.api`) à internet.

## 4. Vulnerabilidades

Relate com os detalhes mínimos para reproduzir, em canal privado ao mantenedor do seu repositório. Não inclua tokens reais.
