from config import INITIAL_BALANCE

class Wallet:

    def __init__(self):
        self.balance = INITIAL_BALANCE

    def get_balance(self):
        return self.balance

    def update_balance(self, new_balance):
        self.balance = new_balance
