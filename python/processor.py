"""
MeetingForge - Processador Python para Electron
Suporta arquivos de longa duração (3h+) com processamento em chunks.
Roda como child process do Electron, comunica via stdout com JSON.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


def send_progress(percent, message, stage="processing"):
    """Envia progresso para o Electron via stdout."""
    data = {"percent": percent, "message": message, "stage": stage}
    print(f"PROGRESS:{json.dumps(data)}", flush=True)


def send_error(message):
    """Envia erro para stderr."""
    print(f"ERROR: {message}", file=sys.stderr, flush=True)


# ─────────────────────────────────────────────
# 0. UTILS - DURAÇÃO E CHUNKS
# ─────────────────────────────────────────────

def get_audio_duration(file_path):
    """Retorna duração do áudio em segundos usando ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def split_audio_chunks(file_path, chunk_duration=1800, output_dir=None):
    """Divide áudio em chunks usando ffmpeg.
    chunk_duration: duração de cada chunk em segundos (padrão: 30min).
    Retorna lista de caminhos dos chunks.
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="mf_chunks_")

    duration = get_audio_duration(file_path)
    if duration is None:
        send_progress(8, "Não foi possível determinar duração, processando arquivo inteiro...", "setup")
        return [file_path], duration

    num_chunks = math.ceil(duration / chunk_duration)

    if num_chunks <= 1:
        return [file_path], duration

    send_progress(8, f"Arquivo longo detectado ({format_duration(duration)}). Dividindo em {num_chunks} partes...", "setup")

    chunks = []
    for i in range(num_chunks):
        start = i * chunk_duration
        chunk_path = os.path.join(output_dir, f"chunk_{i:03d}.wav")

        cmd = [
            "ffmpeg", "-y", "-i", file_path,
            "-ss", str(start), "-t", str(chunk_duration),
            "-ac", "1", "-ar", "16000",  # mono 16kHz para Whisper
            "-acodec", "pcm_s16le",
            "-loglevel", "error",
            chunk_path
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            chunks.append(chunk_path)
        except subprocess.CalledProcessError as e:
            send_error(f"Erro ao dividir chunk {i}: {e.stderr.decode()}")
            raise

        pct = 8 + int((i + 1) / num_chunks * 7)  # 8-15%
        send_progress(pct, f"Dividindo áudio: parte {i+1}/{num_chunks}...", "setup")

    return chunks, duration


def format_duration(seconds):
    """Formata segundos em HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h{m:02d}min"
    return f"{m}min{s:02d}s"


# ─────────────────────────────────────────────
# 1. DOWNLOAD DE VÍDEO
# ─────────────────────────────────────────────

def download_audio(url, output_dir=None):
    """Baixa o áudio de uma URL usando yt-dlp."""
    import yt_dlp

    if output_dir is None:
        output_dir = tempfile.mkdtemp()

    output_path = os.path.join(output_dir, "audio.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
            "preferredquality": "192",
        }],
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [lambda d: send_progress(
            15 if d["status"] == "downloading" else 20,
            f"Baixando áudio... {d.get('_percent_str', '')}".strip(),
            "download"
        )],
    }

    send_progress(10, "Iniciando download do áudio...", "download")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    for f in os.listdir(output_dir):
        if f.startswith("audio"):
            return os.path.join(output_dir, f)

    raise FileNotFoundError("Não foi possível baixar o áudio.")


# ─────────────────────────────────────────────
# 2. TRANSCRIÇÃO COM WHISPER (CHUNKED)
# ─────────────────────────────────────────────

def transcribe_audio(audio_path, model_name="base"):
    """Transcreve áudio usando Whisper, com suporte a arquivos longos via chunks."""
    import whisper

    send_progress(16, f"Carregando modelo Whisper '{model_name}'...", "transcription")
    model = whisper.load_model(model_name)

    # Dividir em chunks para arquivos longos
    chunks, total_duration = split_audio_chunks(audio_path)
    num_chunks = len(chunks)
    is_long = num_chunks > 1

    if is_long and total_duration:
        send_progress(20, f"Transcrevendo {format_duration(total_duration)} em {num_chunks} partes...", "transcription")

    all_segments = []
    time_offset = 0.0
    start_time = time.time()

    for idx, chunk_path in enumerate(chunks):
        chunk_label = f" (parte {idx+1}/{num_chunks})" if is_long else ""
        send_progress(
            20 + int((idx / num_chunks) * 45),
            f"Transcrevendo{chunk_label}... pode levar alguns minutos",
            "transcription"
        )

        result = model.transcribe(
            chunk_path,
            language="pt",
            task="transcribe",
            verbose=False,
            fp16=False,
        )

        # Ajustar timestamps com offset do chunk
        for seg in result["segments"]:
            seg["start"] += time_offset
            seg["end"] += time_offset
            all_segments.append(seg)

        # Calcular offset para próximo chunk
        if chunk_path != audio_path:
            chunk_dur = get_audio_duration(chunk_path)
            if chunk_dur:
                time_offset += chunk_dur

        # Progresso e tempo estimado
        elapsed = time.time() - start_time
        if idx > 0:
            avg_per_chunk = elapsed / (idx + 1)
            remaining = avg_per_chunk * (num_chunks - idx - 1)
            eta = format_duration(remaining)
            send_progress(
                20 + int(((idx + 1) / num_chunks) * 45),
                f"Parte {idx+1}/{num_chunks} concluída. Restante estimado: {eta}",
                "transcription"
            )

    # Limpar chunks temporários
    for chunk_path in chunks:
        if chunk_path != audio_path and os.path.exists(chunk_path):
            try:
                os.remove(chunk_path)
            except OSError:
                pass

    send_progress(65, f"Transcrição concluída! {len(all_segments)} segmentos.", "transcription")
    return {"segments": all_segments}


def format_transcription(result):
    """Formata a transcrição com timestamps."""
    lines = []
    lines.append("═" * 60)
    lines.append("  TRANSCRIÇÃO COMPLETA DA REUNIÃO")
    lines.append(f"  Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    if result["segments"]:
        total_secs = result["segments"][-1].get("end", 0)
        lines.append(f"  Duração: {format_duration(total_secs)}")

    lines.append("═" * 60)
    lines.append("")

    for segment in result["segments"]:
        start = segment["start"]
        hours = int(start // 3600)
        minutes = int((start % 3600) // 60)
        seconds = int(start % 60)

        if hours > 0:
            timestamp = f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"
        else:
            timestamp = f"[{minutes:02d}:{seconds:02d}]"

        text = segment["text"].strip()
        lines.append(f"{timestamp} {text}")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# 3. EXTRAÇÃO DE REQUISITOS
# ─────────────────────────────────────────────

def extract_with_claude(transcription, api_key):
    """Usa Claude API para reorganizar transcrição por tópicos e gerar tasks.
    Para transcrições longas, divide em partes para respeitar limites de tokens.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    # Para transcrições muito longas, truncar se necessário
    # Claude suporta ~200k tokens, mas vamos ser conservadores
    max_chars = 400_000
    if len(transcription) > max_chars:
        send_progress(63, "Transcrição muito longa, otimizando para IA...", "extraction")
        transcription = transcription[:max_chars] + "\n\n[... transcrição truncada por limite ...]"

    # ── Organizar por tópicos ──
    send_progress(67, "Organizando por tópicos...", "extraction")

    topics_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=12000,
        messages=[{"role": "user", "content": f"""Analise esta transcrição de reunião (pode ser muito longa, 3-5 horas) e REORGANIZE TODO o conteúdo por tópicos/assuntos discutidos.

INSTRUÇÕES:
- Agrupe TUDO o que foi dito por tema/assunto, mesmo que o mesmo tema tenha sido discutido em momentos diferentes da reunião
- Mantenha o MÁXIMO de detalhes e contexto original — não resuma excessivamente
- Para cada tópico, inclua:
  - O que foi discutido (com detalhes e contexto original)
  - Decisões tomadas sobre esse tema
  - Perguntas levantadas e se foram respondidas
  - Ações definidas / próximos passos para esse tema
  - Referências de tempo aproximadas [HH:MM] de quando foi discutido
- Ao final, inclua uma seção "RESUMO DE DECISÕES E PENDÊNCIAS" com:
  - Lista de todas as decisões tomadas na reunião
  - Lista de todos os itens pendentes / que ficaram em aberto
  - Lista de ações atribuídas a pessoas específicas (se mencionadas)

Use formatação clara com headers markdown (##) para cada tópico.

TRANSCRIÇÃO:
{transcription}"""}],
    )
    topics = topics_response.content[0].text

    # ── Gerar tasks ──
    send_progress(82, "Gerando tasks para desenvolvimento...", "extraction")

    prompt_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=12000,
        messages=[{"role": "user", "content": f"""Baseado nesta transcrição de reunião, gere uma lista de TASKS (tarefas) independentes e executáveis, prontas para serem usadas em ferramentas de IA como Claude Code ou Cursor.

FORMATO DE CADA TASK:
---
### TASK [número]: [título curto e descritivo]
**Prioridade:** [ALTA/MÉDIA/BAIXA]
**Tipo:** [setup/feature/integração/teste/documentação]
**Dependências:** [lista de números de tasks que precisam estar prontas antes, ou "nenhuma"]

**Contexto:**
[Explicação do que foi discutido na reunião sobre esse item, com detalhes suficientes para alguém que não participou da reunião entender o que precisa ser feito]

**Critérios de aceite:**
- [critério 1]
- [critério 2]
- [...]

**Prompt para Claude Code/Cursor:**
```
[Prompt completo, auto-suficiente, pronto para colar diretamente em uma ferramenta de IA.
O prompt deve conter TODO o contexto necessário para implementar a feature sem precisar
consultar outros documentos. Deve incluir detalhes técnicos, regras de negócio,
e instruções claras de implementação.]
```
---

REGRAS:
1. Comece com tasks de SETUP (ambiente, projeto base, banco de dados)
2. Depois features ordenadas por prioridade e dependências
3. Termine com tasks de integração e testes
4. Cada task deve ser INDEPENDENTE — alguém colando o prompt deve conseguir implementar a feature
5. Os prompts internos devem ser DETALHADOS e AUTO-SUFICIENTES
6. Inclua todas as regras de negócio, validações e detalhes mencionados na reunião

TRANSCRIÇÃO:
{transcription}"""}],
    )
    prompt = prompt_response.content[0].text

    return topics, prompt


def extract_local(transcription):
    """Extração local sem IA externa.
    Divide a transcrição em blocos de ~10 minutos e extrai palavras-chave como tópicos.
    Também gera tasks básicas com prompts para ferramentas de IA.
    """
    send_progress(67, "Organizando por tópicos localmente...", "extraction")

    lines = transcription.split("\n")

    # ── Parse lines with timestamps into timed segments ──
    timestamp_re = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*(.*)")
    timed_lines = []
    for line in lines:
        m = timestamp_re.match(line.strip())
        if m:
            parts = m.groups()
            if parts[2] is not None:
                total_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                total_sec = int(parts[0]) * 60 + int(parts[1])
            text = parts[3].strip()
            if text:
                timed_lines.append((total_sec, text))

    # ── Stopwords for Portuguese keyword extraction ──
    stopwords = {
        "a", "o", "e", "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas",
        "um", "uma", "uns", "umas", "que", "para", "por", "com", "se", "não", "mais",
        "mas", "como", "eu", "ele", "ela", "nós", "eles", "elas", "você", "vocês",
        "isso", "isto", "esse", "essa", "aqui", "ali", "então", "vai", "tem", "ter",
        "ser", "está", "são", "foi", "muito", "bem", "sim", "aí", "né", "lá", "já",
        "assim", "tipo", "coisa", "gente", "porque", "quando", "onde", "qual", "quem",
        "todo", "toda", "todos", "todas", "mesmo", "ainda", "sobre", "até", "depois",
        "antes", "pode", "poder", "fazer", "faz", "ver", "vamos", "ia", "era", "só",
        "tá", "pra", "pro", "pela", "pelo", "entre", "cada", "outro", "outra",
        "outros", "parte", "vezes", "dia", "vez", "coisa", "coisas",
    }

    def extract_keywords(text_block, top_n=5):
        """Extract most frequent meaningful words from a text block."""
        words = re.findall(r"[a-záàâãéèêíïóôõúüç]{4,}", text_block.lower())
        freq = {}
        for w in words:
            if w not in stopwords:
                freq[w] = freq.get(w, 0) + 1
        sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return [w for w, _ in sorted_words[:top_n]]

    def format_time(seconds):
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h:02d}:{m:02d}"

    # ── Group into ~10-minute blocks ──
    block_duration = 600  # 10 minutes in seconds
    blocks = []
    if timed_lines:
        start_sec = timed_lines[0][0]
        block_start = start_sec
        block_texts = []
        for sec, text in timed_lines:
            if sec - block_start >= block_duration and block_texts:
                blocks.append((block_start, sec, block_texts))
                block_start = sec
                block_texts = []
            block_texts.append(text)
        if block_texts:
            end_sec = timed_lines[-1][0]
            blocks.append((block_start, end_sec, block_texts))
    else:
        # Fallback: no timestamps, treat whole transcription as one block
        plain_lines = [l.strip() for l in lines if l.strip() and not l.startswith("═") and not l.startswith(" ")]
        if plain_lines:
            blocks.append((0, 0, plain_lines))

    # ── Build topics output ──
    topics = "=" * 60 + "\n"
    topics += "  TRANSCRIÇÃO ORGANIZADA POR TÓPICOS\n"
    topics += "  (extração local - sem IA)\n"
    topics += "=" * 60 + "\n\n"

    for block_start, block_end, texts in blocks:
        combined = " ".join(texts)
        keywords = extract_keywords(combined)
        keyword_label = ", ".join(keywords) if keywords else "Discussão geral"

        time_range = f"[{format_time(block_start)} - {format_time(block_end)}]"
        topics += f"## {time_range} {keyword_label.title()}\n\n"
        for t in texts:
            topics += f"{t}\n"
        topics += "\n" + "-" * 40 + "\n\n"

    topics += "\n" + "=" * 60 + "\n"
    topics += "NOTA: Para resultados mais detalhados e agrupamento inteligente,\n"
    topics += "   configure sua chave da API Anthropic nas configurações.\n"

    # ── Extract requirements for tasks ──
    send_progress(75, "Gerando tasks para desenvolvimento...", "extraction")

    keywords_req = [
        "precis", "quer", "gostaria", "necessit", "deve", "tem que",
        "import", "funcionalidade", "módulo", "sistema", "tela",
        "relatório", "dashboard", "cadastro", "login", "notificação",
        "integr", "api", "mobile", "responsiv", "perfil", "permiss",
        "pagamento", "fatur", "agenda", "horário", "botão", "página",
    ]

    requirements = []
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in keywords_req):
            clean = re.sub(r"\[\d{2}:\d{2}(:\d{2})?\]\s*", "", line).strip()
            if clean and len(clean) > 15 and clean not in requirements:
                requirements.append(clean)

    # ── Build tasks output ──
    prompt = "=" * 60 + "\n"
    prompt += "  TASKS PARA DESENVOLVIMENTO\n"
    prompt += "  (extração local - sem IA)\n"
    prompt += "=" * 60 + "\n\n"

    # Task 0: Setup
    prompt += "---\n"
    prompt += "### TASK 0: Setup do projeto\n"
    prompt += "**Prioridade:** ALTA\n"
    prompt += "**Tipo:** setup\n"
    prompt += "**Dependências:** nenhuma\n\n"
    prompt += "**Contexto:**\n"
    prompt += "Configurar o ambiente de desenvolvimento e estrutura base do projeto.\n\n"
    prompt += "**Critérios de aceite:**\n"
    prompt += "- Projeto inicializado com a stack escolhida\n"
    prompt += "- Banco de dados configurado\n"
    prompt += "- Autenticação básica funcionando\n\n"
    prompt += "**Prompt para Claude Code/Cursor:**\n"
    prompt += "```\n"
    prompt += "Crie a estrutura inicial de um projeto web fullstack com:\n"
    prompt += "- Next.js 14+ com TypeScript e Tailwind CSS\n"
    prompt += "- Prisma ORM com PostgreSQL\n"
    prompt += "- NextAuth.js para autenticação\n"
    prompt += "- Estrutura de pastas organizada\n"
    prompt += "- Layout base responsivo com sidebar e header\n"
    prompt += "```\n"
    prompt += "---\n\n"

    for i, req in enumerate(requirements, 1):
        prompt += "---\n"
        prompt += f"### TASK {i}: {req[:80]}\n"
        prompt += "**Prioridade:** MÉDIA\n"
        prompt += "**Tipo:** feature\n"
        prompt += f"**Dependências:** TASK 0{f', TASK {i-1}' if i > 1 else ''}\n\n"
        prompt += "**Contexto:**\n"
        prompt += f"Requisito extraído da reunião: {req}\n\n"
        prompt += "**Critérios de aceite:**\n"
        prompt += f"- Funcionalidade implementada conforme descrito\n"
        prompt += "- Interface responsiva\n"
        prompt += "- Validações de entrada\n"
        prompt += "- Tratamento de erros\n\n"
        prompt += "**Prompt para Claude Code/Cursor:**\n"
        prompt += "```\n"
        prompt += f"Implemente a seguinte funcionalidade no projeto existente:\n\n"
        prompt += f"{req}\n\n"
        prompt += "Requisitos técnicos:\n"
        prompt += "- Use TypeScript com tipos bem definidos\n"
        prompt += "- Componentes React com Tailwind CSS para estilização\n"
        prompt += "- Crie as rotas de API necessárias em Next.js API Routes\n"
        prompt += "- Use Prisma para operações de banco de dados\n"
        prompt += "- Adicione validação com Zod nos formulários e APIs\n"
        prompt += "- Design responsivo mobile-first\n"
        prompt += "- Tratamento de erros com feedback visual ao usuário\n"
        prompt += "```\n"
        prompt += "---\n\n"

    # Final task: integration and testing
    final_idx = len(requirements) + 1
    prompt += "---\n"
    prompt += f"### TASK {final_idx}: Integração e testes\n"
    prompt += "**Prioridade:** ALTA\n"
    prompt += "**Tipo:** teste\n"
    prompt += f"**Dependências:** TASK 1 até TASK {len(requirements)}\n\n"
    prompt += "**Contexto:**\n"
    prompt += "Garantir que todos os módulos funcionam integrados e criar testes.\n\n"
    prompt += "**Critérios de aceite:**\n"
    prompt += "- Todos os módulos integrados e funcionando\n"
    prompt += "- Testes unitários nos módulos críticos\n"
    prompt += "- Testes de integração nas rotas de API\n"
    prompt += "- Navegação completa sem erros\n\n"
    prompt += "**Prompt para Claude Code/Cursor:**\n"
    prompt += "```\n"
    prompt += "Revise o projeto completo e:\n"
    prompt += "1. Verifique que todos os módulos estão integrados corretamente\n"
    prompt += "2. Adicione testes unitários com Jest/Vitest para os componentes críticos\n"
    prompt += "3. Adicione testes de integração para as rotas de API\n"
    prompt += "4. Verifique a navegação completa da aplicação\n"
    prompt += "5. Corrija quaisquer erros ou inconsistências encontrados\n"
    prompt += "```\n"
    prompt += "---\n\n"

    prompt += "\n" + "=" * 60 + "\n"
    prompt += "NOTA: Para tasks mais detalhadas e contextualizadas,\n"
    prompt += "   configure sua chave da API Anthropic nas configurações.\n"

    send_progress(82, "Extração concluída!", "extraction")
    return topics, prompt


# ─────────────────────────────────────────────
# 4. SALVAR RESULTADOS
# ─────────────────────────────────────────────

def save_results(transcription, topics, prompt, output_dir):
    """Salva todos os resultados."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(output_dir, f"reuniao_{timestamp}")

    os.makedirs(output_dir, exist_ok=True)

    paths = {}

    paths["transcription"] = f"{base}_transcricao.txt"
    with open(paths["transcription"], "w", encoding="utf-8") as f:
        f.write(transcription)

    paths["topics"] = f"{base}_topicos.txt"
    with open(paths["topics"], "w", encoding="utf-8") as f:
        f.write(topics)

    paths["prompt"] = f"{base}_prompt.txt"
    with open(paths["prompt"], "w", encoding="utf-8") as f:
        f.write(prompt)

    paths["json"] = f"{base}_completo.json"
    with open(paths["json"], "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": timestamp,
            "transcription": transcription,
            "topics": topics,
            "prompt": prompt,
        }, f, ensure_ascii=False, indent=2)

    return paths


# ─────────────────────────────────────────────
# 5. MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--file", "-f")
    input_group.add_argument("--url", "-u")
    parser.add_argument("--model", "-m", default="base",
                        choices=["tiny", "base", "small", "medium", "large"])
    parser.add_argument("--output", "-o", default=".")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--chunk-duration", type=int, default=1800,
                        help="Duração de cada chunk em segundos (padrão: 1800 = 30min)")

    args = parser.parse_args()

    try:
        # Step 1: Get audio
        send_progress(5, "Iniciando processamento...", "setup")

        if args.url:
            audio_path = download_audio(args.url)
        else:
            audio_path = args.file
            if not os.path.exists(audio_path):
                raise FileNotFoundError(f"Arquivo não encontrado: {audio_path}")

            duration = get_audio_duration(audio_path)
            if duration:
                send_progress(6, f"Arquivo carregado: {format_duration(duration)}", "setup")
            else:
                send_progress(6, "Arquivo carregado.", "setup")

        # Step 2: Transcribe (com chunking automático para arquivos longos)
        result = transcribe_audio(audio_path, args.model)
        transcription = format_transcription(result)

        # Step 3: Extract requirements
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not args.no_ai and api_key:
            topics, prompt = extract_with_claude(transcription, api_key)
        else:
            topics, prompt = extract_local(transcription)

        # Step 4: Save
        send_progress(92, "Salvando resultados...", "saving")
        paths = save_results(transcription, topics, prompt, args.output)

        send_progress(100, "Concluído!", "done")

        # Output JSON result for Electron
        if args.json_output:
            result_data = {
                "transcription": transcription,
                "topics": topics,
                "prompt": prompt,
                "files": paths,
            }
            print(f"RESULT_JSON_START{json.dumps(result_data, ensure_ascii=False)}RESULT_JSON_END", flush=True)

    except Exception as e:
        send_error(str(e))
        if args.json_output:
            print(f"RESULT_JSON_START{json.dumps({'error': str(e)})}RESULT_JSON_END", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
