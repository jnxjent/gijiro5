import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.function_name(name="ProcessAudio")
@app.route(route="ProcessAudio", methods=["POST"])
def process_audio(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("ok", status_code=200)
