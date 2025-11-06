import json
import re
from datetime import datetime
import pandas as pd
import numpy as np
import streamlit as st

st.set_page_config(page_title="Organizador de Caixa — Ninja v4", layout="wide")

# ---------------- Helpers ----------------

def brl_to_float(v: str):
    if v is None:
        return None
    s = (str(v)
         .replace('\xa0', '')
         .replace('\u00A0', '')
         .strip()
         .replace('R$', '')
         .replace(' ', '')
         .replace('.', '')
         .replace(',', '.')
    )
    try:
        return float(s)
    except:
        return None

def float_to_brl(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return ""
    # Formata pt-BR visual (para UI)
    return f"R$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def default_depara():
    return {
        "startswith": [
            {"pattern": "Pagto do quarto",     "conta_contabil": "1.01 - Hospedagem", "disponibilidade": "3.1 - HOTEL (CENTRO DE CUSTO)"},
            {"pattern": "Pagamento da comanda","conta_contabil": "1.02 - Frigobar",   "disponibilidade": "3.2 - FRIGOBAR (CENTRO DE CUSTO)"},
        ],
        "contains": [
            {"pattern": "comanda",             "conta_contabil": "1.02 - Frigobar",   "disponibilidade": "3.2 - FRIGOBAR (CENTRO DE CUSTO)"},
        ],
        "regex": []
    }

# ====== Mapa configurável: Forma de pagamento -> Conta Destino ======
def default_pagto_map():
    return [
        {"match": ["vale", "dinheiro"], "destino": "CAIXA"},
        {"match": ["pix", "pix qr code", "cartão de débito", "cartao de debito", "cartão de crédito", "cartao de credito"], "destino": "STONE"},
        {"match": ["faturamento", "boleto"], "destino": "FATURAMENTO"},
    ]

def map_conta_destino(forma_pagamento: str, mapa):
    s = (str(forma_pagamento) or "").strip().lower()
    for regra in mapa:
        for token in regra.get("match", []):
            if token in s:
                return regra.get("destino", "OUTROS")
    return "OUTROS"

def apply_rule(df, mask, conta, disp):
    if conta:
        df.loc[mask, "Conta Contábil"] = conta
    if disp:
        df.loc[mask, "Disponibilidade"] = disp
    return df

def apply_depara(df_mov, depara, source_col="Descrição_base"):
    df = df_mov.copy()
    if "Conta Contábil" not in df.columns:
        df["Conta Contábil"] = ""
    if "Disponibilidade" not in df.columns:
        df["Disponibilidade"] = ""

    col = source_col if source_col in df.columns else "Descrição"

    # startswith
    for rule in depara.get("startswith", []):
        pat = (rule.get("pattern") or "").strip()
        if pat:
            mask = df[col].str.startswith(pat, na=False)
            df = apply_rule(df, mask, rule.get("conta_contabil",""), rule.get("disponibilidade",""))

    # contains
    for rule in depara.get("contains", []):
        pat = (rule.get("pattern") or "").strip()
        if pat:
            mask = df[col].str.contains(pat, case=False, na=False)
            df = apply_rule(df, mask, rule.get("conta_contabil",""), rule.get("disponibilidade",""))

    # regex
    for rule in depara.get("regex", []):
        pat = (rule.get("pattern") or "").strip()
        if pat:
            flags = re.IGNORECASE if rule.get("ignore_case", True) else 0
            try:
                rx = re.compile(pat, flags)
                mask = df[col].astype(str).apply(lambda s: bool(rx.search(s)))
                df = apply_rule(df, mask, rule.get("conta_contabil",""), rule.get("disponibilidade",""))
            except re.error:
                pass
    return df

# -------- Resumo: aceita campos extras --------
def parse_resumo(raw: str):
    cabeca = {}
    for key in ["Caixa", "Usuário", "Abertura", "Fechamento"]:
        m = re.search(rf"{key}:\s*([^\n\r]+?)\s(?=(\w+:)|$)", raw + " FIM:", flags=re.IGNORECASE)
        if m:
            cabeca[key] = m.group(1).strip()

    campos = [
        "Crédito","Dinheiro bruto","Faturamento","Fechamento","Saldo bruto",
        "Débito","Fundo de caixa","Prazo","A prazo","Estornos","Total despesas",
        "PIX","Dinheiro líquido","Cheque","Estornado","Total sangria",
        "PIX QR CODE","Hospedagens (dinheiro)","Total boleto","A devolver","Total a devolver","Saldo líquido",
        "Transferência bancária","Total Hospedagens (outras formas)","Desconto","Descontos",
        "Depósito","Total Consumos","Total serviços","Vendas PDV","Total reforço"
    ]
    for c in campos:
        m = re.search(rf"{re.escape(c)}:?\s*R\$\s?[\d\.\,]+", raw, flags=re.IGNORECASE)
        if m:
            parte = m.group(0)
            valor = parte.split("R$")[-1].strip()
            cabeca[c] = f"R$ {valor}"

    if "Caixa" in cabeca:
        try:
            cabeca["Caixa"] = int(re.sub(r"\D", "", cabeca["Caixa"]))
        except:
            pass
    return cabeca

# -------- Movimentos: aceita cabeçalho com/sem 'Usuário' e 'Desconto' --------
def parse_movimentos(raw: str, concat_codigo=True):
    header_rx = r"Código mov\.\s+Descrição\s+(Usuário\s+)?Tipo\s+Valor\s+Forma pagamento\s+Num\. parcelas\s+Lançamento(.*)$"
    tbl_match = re.search(header_rx, raw, flags=re.S | re.IGNORECASE)
    if not tbl_match:
        return pd.DataFrame(columns=[
            "Código mov.","Descrição_base","Descrição","Tipo","Valor","Forma pagamento","Num. parcelas","Lançamento"
        ])

    tabela_blob = tbl_match.group(2).strip()
    linhas = [l.strip() for l in re.split(r"[\r\n]+", tabela_blob) if l.strip()]
    regs = []

    for ln in linhas:
        parts = re.split(r"\t+", ln)
        if len(parts) < 7:
            parts = re.split(r"\s{2,}", ln)

        cod = desc = tipo = valor = forma = parcelas = lanc = None

        if len(parts) >= 8:
            cod, desc, _usuario, tipo, valor, forma, parcelas, lanc = (
                parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(),
                parts[4].strip(), parts[5].strip(), parts[6].strip(), " ".join(parts[7:]).strip()
            )
        elif len(parts) >= 7:
            cod, desc, tipo, valor, forma, parcelas, lanc = (
                parts[0].strip(), parts[1].strip(), parts[2].strip(),
                parts[3].strip(), parts[4].strip(), parts[5].strip(),
                " ".join(parts[6:]).strip()
            )
        else:
            m = re.match(
                r"^(\d+)\s+(.*?)\s+(Reforço|Entrada|Sa[ií]da|Desconto)\s+(R\$\s?[\d\.\,]+)\s+(.*?)\s+(\d+)\s+(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})$",
                ln, flags=re.IGNORECASE
            )
            if not m:
                continue
            cod, desc, tipo, valor, forma, parcelas, lanc = m.groups()

        try:
            cod_int = int(re.sub(r"\D", "", cod))
        except:
            cod_int = None

        try:
            parcelas_int = int(re.sub(r"\D", "", parcelas))
        except:
            parcelas_int = None

        desc_base = desc
        desc_show = f"{cod_int} - {desc}" if concat_codigo else desc

        regs.append({
            "Código mov.": cod_int,
            "Descrição_base": desc_base,
            "Descrição": desc_show,
            "Tipo": tipo,
            "Valor": valor,
            "Forma pagamento": forma,
            "Num. parcelas": parcelas_int,
            "Lançamento": lanc
        })

    return pd.DataFrame(regs, columns=[
        "Código mov.","Descrição_base","Descrição","Tipo","Valor","Forma pagamento","Num. parcelas","Lançamento"
    ])

def add_subtotals_in_detail(df_sorted, group_cols):
    df = df_sorted.copy()
    if "Valor_num" not in df.columns:
        df["Valor_num"] = df["Valor"].apply(brl_to_float)

    linhas = []
    cols = ["Código mov.", "Descrição", "Tipo", "Valor", "Valor_num", "Forma pagamento",
            "Num. parcelas", "Lançamento", "Conta Contábil", "Disponibilidade"]

    def append_group(gdf, level=0):
        nonlocal linhas
        if level == len(group_cols):
            for _, row in gdf.iterrows():
                linhas.append({c: row.get(c, "") for c in cols})
            return
        col = group_cols[level]
        for key, sub in gdf.groupby(col, dropna=False):
            append_group(sub, level+1)
            subtotal = sub["Valor_num"].sum()
            key_txt = "" if (key is None or (isinstance(key,float) and np.isnan(key))) else str(key)
            linhas.append({
                "Código mov.": "",
                "Descrição": f"Subtotal - {col}: {key_txt}",
                "Tipo": "",
                "Valor": float_to_brl(subtotal),
                "Valor_num": subtotal,
                "Forma pagamento": sub["Forma pagamento"].iloc[0] if "Forma pagamento" in sub.columns and len(sub)>0 else "",
                "Num. parcelas": "",
                "Lançamento": "",
                "Conta Contábil": "",
                "Disponibilidade": ""
            })

    append_group(df)
    grand_total = df["Valor_num"].sum()
    linhas.append({
        "Código mov.": "",
        "Descrição": "TOTAL GERAL",
        "Tipo": "",
        "Valor": float_to_brl(grand_total),
        "Valor_num": grand_total,
        "Forma pagamento": "",
        "Num. parcelas": "",
        "Lançamento": "",
        "Conta Contábil": "",
        "Disponibilidade": ""
    })
    return pd.DataFrame(linhas, columns=cols)

# ---------------- State ----------------
if "depara" not in st.session_state:
    st.session_state.depara = default_depara()
if "pagto_map" not in st.session_state:
    st.session_state.pagto_map = default_pagto_map()

st.title("🥷 Organizador de Caixa — Ninja v4")
st.caption("Lançamentos contábeis linha a linha + subtotais por Conta Destino. Excel com ponto na aba de Detalhe.")

with st.expander("➕ Instruções rápidas", expanded=False):
    st.markdown("""
    1. **Cole** o conteúdo bruto (ou faça upload de .txt).
    2. Ajuste o **De/Para** (startswith/contains/regex) e o **Mapa Conta Destino** (por forma de pagamento).
    3. Clique em **Gerar prévia** e depois **Exportar Excel**.
    """)

# -------- Sidebar: De/Para + Mapa Conta Destino --------
st.sidebar.header("⚙️ De/Para (classificação)")
st.sidebar.markdown("---")
if st.sidebar.button("🔄 Resetar De/Para (padrão)"):
    st.session_state.pop("depara", None)
    st.rerun()

uploaded_depara = st.sidebar.file_uploader("Importar De/Para (JSON)", type=["json"])
if uploaded_depara is not None:
    try:
        st.session_state.depara = json.load(uploaded_depara)
        st.sidebar.success("De/Para importado!")
    except Exception as e:
        st.sidebar.error(f"Erro ao importar De/Para: {e}")

def render_rules_block(title, key_name):
    st.sidebar.subheader(title)
    rules = st.session_state.depara.get(key_name, [])
    to_remove = []
    for i, r in enumerate(rules):
        with st.sidebar.expander(f"{title} #{i+1}", expanded=False):
            r["pattern"] = st.text_input(f"Pattern #{i+1}", r.get("pattern",""), key=f"{key_name}_pat_{i}")
            r["conta_contabil"] = st.text_input(f"Conta Contábil #{i+1}", r.get("conta_contabil",""), key=f"{key_name}_cc_{i}")
            r["disponibilidade"] = st.text_input(f"Disponibilidade #{i+1}", r.get("disponibilidade",""), key=f"{key_name}_disp_{i}")
            if key_name == "regex":
                r["ignore_case"] = st.checkbox("Ignorar maiúsc./minúsc.", value=r.get("ignore_case", True), key=f"{key_name}_ic_{i}")
            if st.button("Remover", key=f"{key_name}_rm_{i}"):
                to_remove.append(i)
    for idx in reversed(to_remove):
        rules.pop(idx)
    if st.sidebar.button(f"➕ Adicionar {title}"):
        new_rule = {"pattern":"", "conta_contabil":"", "disponibilidade":""}
        if key_name == "regex":
            new_rule["ignore_case"] = True
        rules.append(new_rule)
    st.session_state.depara[key_name] = rules

render_rules_block("Regras por início (startswith)", "startswith")
render_rules_block("Regras por conteúdo (contains)", "contains")
render_rules_block("Regras por Regex (regex)", "regex")

st.sidebar.download_button("⬇️ Baixar De/Para (JSON)",
                           data=json.dumps(st.session_state.depara, ensure_ascii=False, indent=2),
                           file_name="depara.json",
                           key="download_depara")

st.sidebar.header("🏦 Mapa Conta Destino (Forma de Pagamento → Conta)")
uploaded_map = st.sidebar.file_uploader("Importar Mapa (JSON)", type=["json"])
if uploaded_map is not None:
    try:
        st.session_state.pagto_map = json.load(uploaded_map)
        st.sidebar.success("Mapa importado!")
    except Exception as e:
        st.sidebar.error(f"Erro ao importar Mapa: {e}")

with st.sidebar.expander("Editar Mapa (itens)", expanded=False):
    mapa = st.session_state.pagto_map
    remove_idx = []
    for i, regra in enumerate(mapa):
        st.markdown(f"**Regra #{i+1}**")
        destino = st.text_input(f"Destino #{i+1}", regra.get("destino",""), key=f"map_dest_{i}")
        match_raw = st.text_input(f"Match tokens (separar por ; ) #{i+1}", "; ".join(regra.get("match", [])), key=f"map_tokens_{i}")
        mapa[i]["destino"] = destino
        mapa[i]["match"] = [t.strip().lower() for t in match_raw.split(";") if t.strip()]
        if st.button(f"Remover regra #{i+1}", key=f"map_rm_{i}"):
            remove_idx.append(i)
        st.divider()
    for idx in reversed(remove_idx):
        mapa.pop(idx)
    if st.button("➕ Adicionar Regra"):
        mapa.append({"destino":"", "match":[]})
    st.session_state.pagto_map = mapa

st.sidebar.download_button("⬇️ Baixar Mapa (JSON)",
                           data=json.dumps(st.session_state.pagto_map, ensure_ascii=False, indent=2),
                           file_name="mapa_conta_destino.json",
                           key="download_mapa")

# -------- Input --------
raw = st.text_area("Cole aqui o conteúdo do caixa", height=260, placeholder="Cole o texto começando por 'Caixa: ...' seguido do quadro de totais e da tabela de movimentos...")
upload_txt = st.file_uploader("Ou envie um arquivo .txt com o conteúdo", type=["txt"])
if upload_txt is not None and not raw.strip():
    try:
        raw = upload_txt.read().decode("utf-8", errors="ignore")
        st.success("Arquivo carregado!")
    except Exception as e:
        st.error(f"Erro ao ler arquivo: {e}")

# -------- Options --------
all_group_cols = ["Forma pagamento", "Tipo"]
group_cols = st.multiselect("Agrupar por (ordem importa)", options=all_group_cols, default=["Forma pagamento"])
concat_codigo = st.checkbox("Concatenar código do movimento na descrição", value=True)

btn = st.button("🚀 Gerar prévia")

if btn and raw.strip():
    try:
        resumo = parse_resumo(raw)
        df_resumo = pd.DataFrame([resumo]) if resumo else pd.DataFrame()

        df_mov = parse_movimentos(raw, concat_codigo=concat_codigo)

        # aplica de/para (usa Descrição_base)
        df_mov = apply_depara(df_mov, st.session_state.depara, source_col="Descrição_base")

        # ordenar por grupos + Lançamento
        sort_cols = group_cols + ["Lançamento"]
        df_mov = df_mov.sort_values(by=sort_cols).reset_index(drop=True)

        # detalhe com subtotais
        df_detalhe = add_subtotals_in_detail(df_mov, group_cols)

        # ----- Lançamentos (Contábil) linha a linha -----
        df_contabil = df_mov.copy()
        df_contabil = df_contabil[~df_contabil["Descrição"].str.contains("Abertura automática", case=False, na=False)].copy()

        # Conta Destino (via mapa configurável)
        df_contabil["Valor_num"] = df_contabil["Valor"].apply(brl_to_float)
        df_contabil["Conta Destino"] = df_contabil["Forma pagamento"].apply(lambda x: map_conta_destino(x, st.session_state.pagto_map))

        # Data simples
        def only_date(s):
            try:
                return str(s).split(" ")[0]
            except:
                return s
        df_contabil["Data"] = df_contabil["Lançamento"].astype(str).apply(only_date)

        df_contabil_out = df_contabil[[
            "Descrição", "Conta Contábil", "Disponibilidade", "Data", "Valor_num", "Conta Destino"
        ]].rename(columns={"Valor_num": "Valor"})

        # Subtotais por Conta Destino (UI apenas)
        df_contabil_sub = (
            df_contabil
              .groupby("Conta Destino", dropna=False, as_index=False)
              .agg(Quantidade=("Descrição", "count"),
                   Total_Valor=("Valor_num", "sum"))
              .sort_values("Conta Destino")
        )
        df_contabil_sub_fmt = df_contabil_sub.copy()
        df_contabil_sub_fmt["Total_Valor"] = df_contabil_sub_fmt["Total_Valor"].apply(float_to_brl)

        # ---- Prévia ----
        st.subheader("Prévia – Resumo Caixa")
        st.dataframe(df_resumo, use_container_width=True)

        st.subheader("Prévia – Movimentos (Detalhe) com quebras/subtotais")
        st.dataframe(df_detalhe, use_container_width=True)

        st.subheader("Prévia – Lançamentos (Contábil) – linha a linha")
        df_contabil_preview = df_contabil_out.copy()
        df_contabil_preview["Valor"] = df_contabil_preview["Valor"].apply(float_to_brl)
        st.dataframe(df_contabil_preview, use_container_width=True)

        st.subheader("Prévia – Subtotais por Conta Destino")
        st.dataframe(df_contabil_sub_fmt, use_container_width=True)

        # ---- Exportar Excel (3 abas) ----
        from io import BytesIO
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            # 1) Resumo
            df_resumo.to_excel(writer, sheet_name="Resumo Caixa", index=False)

            # 2) Movimentos (Detalhe) — Valor como TEXTO com ponto; mantém Valor_num numérico
            df_detalhe_x = df_detalhe.copy()
            if "Valor_num" in df_detalhe_x.columns:
                df_detalhe_x["Valor"] = df_detalhe_x["Valor_num"].apply(
                    lambda x: "" if pd.isna(x) else f"{x:.2f}"
                )
            df_detalhe_x.to_excel(writer, sheet_name="Movimentos (Detalhe)", index=False)

            from openpyxl.styles import numbers
            ws_md = writer.sheets["Movimentos (Detalhe)"]
            cols_md = {c: i for i, c in enumerate(df_detalhe_x.columns, start=1)}
            col_idx_md = cols_md.get("Valor")
            if col_idx_md:
                for row in range(2, len(df_detalhe_x) + 2):
                    cell = ws_md.cell(row=row, column=col_idx_md)
                    cell.number_format = numbers.FORMAT_TEXT  # força texto (@)

            # 3) Lançamentos (Contábil) — numérico com 0.00
            df_contabil_out.to_excel(writer, sheet_name="Lançamentos (Contábil)", index=False)
            ws_lc = writer.sheets["Lançamentos (Contábil)"]
            cols_lc = {c: i for i, c in enumerate(df_contabil_out.columns, start=1)}
            col_idx_lc = cols_lc.get("Valor")
            if col_idx_lc:
                for row in range(2, len(df_contabil_out) + 2):
                    cell = ws_lc.cell(row=row, column=col_idx_lc)
                    cell.number_format = '0.00'  # ponto como separador decimal

        st.download_button(
            "⬇️ Exportar Excel",
            data=output.getvalue(),
            file_name="movimento_processado.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_excel_main"
        )

    except Exception as e:
        st.error(f"Falha ao processar: {e}")
else:
    st.info("Cole o conteúdo (ou envie um .txt) e clique em **Gerar prévia** para continuar.")
