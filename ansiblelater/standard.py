class Standard(object):
    """
    Standard definition for all defined rules.

    Later lookup the config file for a path to a rules directory
    or fallback to default `ansiblelater/data/*`.
    """

    def __init__(self, standard_dict):
        """
        Initialize a new standard object and returns None.

        :param standard_dict: Dictionary object containing all neseccary attributes

        """
        if "id" not in standard_dict:
            standard_dict.update(id="")
        else:
            standard_dict.update(id="[{}] ".format(standard_dict.get("id")))

        self.id = standard_dict.get("id")
        self.name = standard_dict.get("name")
        self.version = standard_dict.get("version")
        self.check = standard_dict.get("check")
        self.types = standard_dict.get("types")


    def __repr__(self): # noqa
        return "Standard: %s (version: %s, types: %s)" % (
               self.name, self.version, self.types)

