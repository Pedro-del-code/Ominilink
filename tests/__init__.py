import logging

# evita a saída de "último recurso" do logging nos testes que não configuram o logger do projeto
logging.getLogger("omnilink").addHandler(logging.NullHandler())
