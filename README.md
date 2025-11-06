
# Organizador de Caixa — Ninja v4

- Aba contábil **linha a linha** com **Conta Destino** (mapeada por forma de pagamento, configurável via JSON na UI).
- **Subtotais** por Conta Destino **apenas na prévia** (não cria nova aba no Excel).
- Excel continua com:
  1. Resumo Caixa
  2. Movimentos (Detalhe)
  3. Lançamentos (Contábil)

Rodar:
```bash
pip install -r requirements.txt
python -m streamlit run app.py
```
