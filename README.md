# 3DX — laboratório público Blender

Laboratório de **testes de desenvolvimento** do sistema 3DX. Não inclui código, fotos, tokens, modelos ou outros ativos privados do ANGELS-NIGHT-WORLD.

## Como testar

1. Acesse [Actions](../../actions/workflows/3dx-blender-ci.yml).
2. Selecione **3DX - Testes Blender** e clique em **Run workflow**.
3. Selecione anime, semi_realistic ou realistic.
4. Abra a execução concluída e, em **Artifacts**, baixe `3dx-blender-smoke-...`, com GLB, BLEND, relatório do gerador e validação GLB.

O workflow só roda sob acionamento manual, para não desperdiçar minutos. Usa runner padrão Ubuntu e instala Blender do repositório do sistema. Em repositório público, a execução padrão é gratuita de acordo com os limites e regras do GitHub. Evite habilitar runners pagos.

## O que esse teste prova

- Blender consegue criar um humanoide sintético articulado e exportar `.glb` e `.blend`.
- Um validador verifica cabeçalho GLB, malha, skin, esqueleto e conta animações.
- O processo ocorre sem executar Blender no Render.

**O que NÃO prova:** reconstrução fotorrealista de fotos, capacidade de atender usuários online, modelos AAA ou integração de resultados automática no painel. O Github Actions **não** deve ser usado como backend de geração 3D sob demanda.

## Segurança

Este repositório é **público**. Nunca envie imagens pessoais, segredos, código privado do Angels Night ou modelos licenciados. Os artefatos do workflow são de teste sintético. Não conecte diretamente o Render a esse workflow para processar requisições de visitantes.

## Próxima fase

Após validar a exportação, evoluir o gerador e investigar um worker gratuito apropriado para trabalho pesado. A aplicação principal deve continuar no endereço do Render.
