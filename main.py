from tisService import TisService
from casService import CasService
import time
if __name__ == '__main__':
    tis = TisService()
    tis.Login()
    tis.LoginTIS()
    tis.queryGPA()
    tis.Logout()
