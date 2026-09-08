from dotenv import load_dotenv
load_dotenv("/Users/darshans/Downloads/qc/.env")
import auth
print(auth.issue_session({"email": "darshan@spotdraft.com", "name": "Darshan"}))
