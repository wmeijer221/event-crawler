from ollama import Client
from ollama import ChatResponse
import json
import subprocess
import time
import regex as re
import httpx


class ChatToJson:
    def __init__(self, model: str, timeout: int = 1200, max_retries_on_exception: int = 2):
        self._model = model
        self._max_retries_on_exception = max_retries_on_exception
        self._client = Client(timeout=timeout)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.end()

    def start(self) -> None:
        command = ['ollama', 'serve']
        self._proc = subprocess.Popen(command)
        time.sleep(2)

    def end(self) -> None:
        self._client.close()
        self._proc.kill()
        self._proc.wait()

    def handle(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
        assistant_prompt: str | None = None,
        options: dict | None = None
    ) -> dict[str, str] | list[dict[str, str]]:
        messages = list()
        if system_prompt is not None:
            messages.append({
                'role': 'system',
                'content': system_prompt
            })
        if assistant_prompt is not None:
            messages.append({
                'role': 'assistant',
                'content': assistant_prompt
            })
        messages.append({
            'role': 'user',
            'content': user_prompt,
        })

        if options is None:
            options = {
                "top_p": 0.9,
                "num_predict": 10000
            }

        for _ in range(self._max_retries_on_exception):
            try:
                response: ChatResponse = self._client.chat(
                    model=self._model,
                    messages=messages,
                    options=options
                )
                j_data = response.message.content
                pattern = r'\\u(?![0-9a-fA-F]{4})'
                cleaned_json = re.sub(pattern, r'\\\\u', j_data)
                data = json.loads(cleaned_json)
                # Sometimes it's silly and outputs an array of arrays...
                # So we flatten the results.
                data_ = list()
                for ele in data:
                    if isinstance(ele, list):
                        data_.extend(ele)
                    else:
                        data_.append(ele)
                break
            except json.JSONDecodeError as ex:
                print(ex)
                continue
            except httpx.ReadTimeout as ex:
                print(ex)
                continue
        return data_
