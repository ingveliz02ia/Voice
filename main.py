import uvicorn
from server import app


#If you want to use the server for "reload" you should change "app" and put "server:app" and then add
#reload=True.
if __name__ == '__main__':
	uvicorn.run(app, host="https://voice-production-2147.up.railway.app", port=80)
