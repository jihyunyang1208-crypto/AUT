import os

def load_api_keys(token_dir="kiwoom_api_token"):
    """
    지정된 디렉토리에서 appkey와 secretkey를 읽어옵니다.

    Parameters:
        token_dir (str): 키 파일이 저장된 디렉토리 경로

    Returns:
        tuple: (appkey, secretkey)

    Raises:
        FileNotFoundError: appkey 또는 secretkey 파일이 없을 경우
    """
    appkey_path = None
    secretkey_path = None

    for fname in os.listdir(token_dir):
        if "appkey" in fname.lower():
            appkey_path = os.path.join(token_dir, fname)
        elif "secretkey" in fname.lower():
            secretkey_path = os.path.join(token_dir, fname)

    if not appkey_path or not secretkey_path:
        raise FileNotFoundError("appkey 또는 secretkey 파일을 찾을 수 없습니다.")

    with open(appkey_path, 'r') as f:
        appkey = f.read().strip()
    with open(secretkey_path, 'r') as f:
        secretkey = f.read().strip()

    return appkey, secretkey
