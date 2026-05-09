from fastapi import FastAPI

app = FastAPI(title="Diomede Orchestrator")


@app.get("/")
async def hello_world():
    return {"message": "Hello World from Diomede Orchestrator"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
