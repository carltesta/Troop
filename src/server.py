"""
    Troop Server
    ------------

    Real-time collaborative Live Coding with FoxDot and SuperCollder.

    Sits on a machine (can be a performer machine) and listens for incoming
    connections and messages and distributes these to other connected peers.

"""

from __future__ import absolute_import

try:
    import socketserver
except ImportError:
    import SocketServer as socketserver

try:
    import queue
except:
    import Queue as queue

import socket
import sys
import time
import os.path
import json

from datetime import datetime
from time import sleep
from getpass import getpass
from hashlib import md5
from threading import Thread

from .threadserv import ThreadedServer
from .message import *
from .interpreter import *
from .config import *
from .utils import *
from .ot.server import Server as OTServer, MemoryBackend
from .ot.text_operation import TextOperation, IncompatibleOperationError as OTError


class TroopServer(OTServer):
    """
        This the master Server instance. Other peers on the
        network connect to it and send their keypress information
        to the server, which then sends it on to the others
    """
    bytes  = 2048
    def __init__(self, port=57890, log=False, debug=False):

        # Operation al transform info

        OTServer.__init__(self, "", MemoryBackend())
        self.peer_tag_doc = ""
          
        # Address information
        self.hostname = str(socket.gethostname())

        # Listen on any IP
        self.ip_addr  = "0.0.0.0"
        self.port     = int(port)

        # Public ip for server is the first IPv4 address we find, else just show the hostname
        self.ip_pub = self.hostname

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        self.ip_pub = s.getsockname()[0]
        s.close()

        # Look for an empty port
        port_found = False
        
        while not port_found:

            try:

                self.server = ThreadedServer((self.ip_addr, self.port), TroopRequestHandler)
                port_found  = True

            except socket.error:

                self.port += 1

        # Reference to the thread that is listening for new connections
        self.server_thread = Thread(target=self.server.serve_forever)

        self.waiting_for_ack = False # Flagged True after new connected client
        
        # Dict of IDs to Client instances
        self.clients = {}

        # ID numbers
        self.max_id  = len(PEER_CHARS) - 1
        self.last_id = -1

        # Give request handler information about this server
        TroopRequestHandler.master = self

        # Set a password for the server
        try:

            self.password = md5(getpass("Password (leave blank for no password): ").encode("utf-8"))

        except KeyboardInterrupt:

            sys.exit("Exited")

        # Set up a char queue
        self.msg_queue = queue.Queue()
        self.msg_queue_thread = Thread(target=self.update_send)

        # Set up log for logging a performance

        if log:
            
            # Check if there is a logs folder, if not create it

            log_folder = os.path.join(ROOT_DIR, "logs")

            if not os.path.exists(log_folder):

                os.mkdir(log_folder)

            # Create filename based on date and times
            
            self.fn = time.strftime("server-log-%d%m%y_%H%M%S.txt", time.localtime())
            path    = os.path.join(log_folder, self.fn)
            
            self.log_file   = open(path, "w")
            self.is_logging = True
            
        else:

            self.is_logging = False
            self.log_file = None

    def get_client_from_addr(self, client_address):
        """ Returns the server-side representation of a client
            using the client address tuple """
        for client in list(self.clients.values()):
            if client == client_address:
                return client

    def get_client(self, client_id):
        """ Returns the client instance based on the id  """
        return self.clients[client_id]

    def get_client_locs(self):
        return { int(client.id): int(client.index) for client in list(self.clients.values()) }

    def get_client_ranges(self):
        """ Converts the peer_tag_doc into pairs of tuples to be reconstructed by the client """
        if len(self.peer_tag_doc) == 0:
            return []
        else:
            data = []
            p_char = self.peer_tag_doc[0]
            count = 1
            for char in self.peer_tag_doc[1:]:
                if char != p_char:
                    data.append((get_peer_id_from_char(p_char), int(count)))
                    p_char = char
                    count = 1
                else:
                    count += 1
            if count > 0:
                data.append((get_peer_id_from_char(p_char), int(count)))
            return data


    def get_contents(self):
        return [self.document, self.get_client_ranges(), self.get_client_locs()]

    # Operation info
    # ==============

    def handle_operation(self, message):
        """ Handles a new MSG_OPERATION by updating the document, performing operational transformation
            (if necessary) on it and storing it. """
        
        # Apply to document
        try:
            op = self.receive_operation(message["src_id"], message["revision"], TextOperation(message["operation"]))
        except OTError as err:
            print(self.document, message["operation"])
            raise err
        
        message["operation"] = op.ops

        # Apply to peer tags
        peer_op = TextOperation([get_peer_char(message["src_id"]) * len(val) if isinstance(val, str) else val for val in op.ops])
        self.peer_tag_doc = peer_op(self.peer_tag_doc)

        # Get location of peer
        client = self.clients[message["src_id"]]
        client.set_index(get_operation_index(message["operation"]))

        return message

    def set_contents(self, data):
        """ Updates the document contents, including the location of user text ranges and marks """
        for key, value in data.items():
            self.contents[key] = value
        return

    def start(self):

        self.running = True
        self.server_thread.start()
        self.msg_queue_thread.start()

        stdout("Server running @ {} on port {}\n".format(self.ip_pub, self.port))

        while True:

            try:

                sleep(0.5)

            except KeyboardInterrupt:

                stdout("\nStopping...\n")

                self.kill()

                break
        return

    def get_next_id(self):
        """ Increases the ID counter and returns it. If it goes over the maximum number allowed, it tries to go to back to the start and 
            checks if that client is connected. If all clients are connected, it returns -1, signalling the client to terminate """
        if self.last_id < self.max_id:
            self.last_id += 1
        else:
            for n in list(range(self.last_id, self.max_id)) + list(range(self.last_id)):
                if n not in self.clients:
                    self.last_id = n
            else:
                return -2 # error message for max clients exceeded                   
        return self.last_id

    def clear_history(self):
        """ Removes revision history - make sure clients' revision numbers reset """
        self.backend = MemoryBackend()
        return

    def wait_for_ack(self):
        """ Sets flag to disregard messages that are not MSG_CONNECT_ACK until all clients have responded """
        self.waiting_for_ack = True
        self.acknowledged_clients = []
        return

    def connect_ack(self, message):
        """ Handle response from clients confirming the new connected client """
        client_id = message["src_id"]
        self.acknowledged_clients.append(client_id)
        if all([client_id in self.acknowledged_clients for client_id in self.clients.keys()]):
            self.waiting_for_ack = False
            self.acknowledged_clients = []
        return

    @staticmethod
    def read_configuration_file(filename):
        conf = {}
        with open(filename) as f:
            for line in f.readlines():
                line = line.strip().split("=")
                conf[line[0]] = line[1]
        return conf['host'], int(conf['port'])

    def update_send(self):
        """ This continually sends any operations to clients
        """

        while self.running:

            try:

                msg = self.msg_queue.get_nowait()

                # If logging is set to true, store the message info

                if self.is_logging:

                    self.log_file.write("%.4f" % time.clock() + " " + repr(str(msg)) + "\n")

                # Store the response of the messages
                
                if isinstance(msg, MSG_OPERATION):

                    msg = self.handle_operation(msg)

                self.respond(msg)

            except queue.Empty:

                sleep(0.01)

        return

    def respond(self, msg):
        """ Update all clients with a message. Only sends back messages to
            a client if the `reply` flag is nonzero. """

        for client in list(self.clients.values()):

            try:

                # Send to all other clients and the sender if "reply" flag is true

                if (client.id != msg['src_id']) or ('reply' not in msg.data) or (msg['reply'] == 1):

                    client.send(msg)

            except DeadClientError as err:

                # Remove client if no longer contactable

                self.remove_client(client.id)

                stdout(err)

        return

    def remove_client(self, client_id):

        # Remove from list(s)
            
        if client_id in self.clients:

            del self.clients[client_id]

        # Notify other clients

        for client in list(self.clients.values()):
                
            client.send(MSG_REMOVE(client_id))

        return
        
    def kill(self):
        """ Properly terminates the server """
        if self.log_file is not None: self.log_file.close()

        outgoing = MSG_KILL(-1, "Warning: Server manually killed by keyboard interrupt. Please close the application")

        for client in list(self.clients.values()):

            client.send(outgoing)

        sleep(0.5)
        
        self.running = False
        self.server.shutdown()
        self.server.server_close()
        
        return

    def write(self, string):
        """ Replaces sys.stdout """
        if string != "\n":

            outgoing = MSG_RESPONSE(-1, string)

            for client in list(self.clients.values()):
                
                client.send(outgoing)
                    
        return

# Request Handler for TroopServer 

class TroopRequestHandler(socketserver.BaseRequestHandler):
    master = None

    def client(self):        
        return self.get_client(self.get_client_id())

    def get_client(self, client_id):
        return self.master.get_client(client_id)

    def get_client_id(self):
        return self.client_id

    def authenticate(self, password):
        
        if password == self.master.password.hexdigest():

            # Reply with the client id

            self.client_id = self.master.get_next_id()

            stdout("New Connection from {}".format(self.client_address[0]))

        else:

            # Negative ID indicates failed login

            stdout("Failed login from {}".format(self.client_address[0]))

            self.client_id = -1

        # Send back the user_id as a 4 digit number

        reply = "{:04d}".format( self.client_id ).encode()

        self.request.send(reply)

        return self.client_id

    def not_authenticated(self):
        return self.authenticate(self.get_message()[0]['password']) < 0

    def get_message(self):
        data = self.request.recv(self.master.bytes) 
        data = self.reader.feed(data)
        return data

    def handle_client_lost(self):
        """ Terminates cleanly """
        stdout("Client @ {} has disconnected".format(self.client_address))
        self.master.remove_client(self.client_id)
        return

    def handle_connect(self, msg):
        """ Stores information about the new client. Wait for acknowledgement from all connected peers before continuing processing messages """
        assert isinstance(msg, MSG_CONNECT)

        if self.client_address not in list(self.master.clients.values()):
            
            self.master.wait_for_ack() # Only listen for acknowledge messages

            new_client = Client(self.client_address, self.get_client_id(), self.request, name=msg['name'])
           
            self.connect_clients(new_client) # Contacts other clients
           
            return new_client

    def leader(self):
        """ Returns the peer client that is "leading" """
        return self.master.leader()
    
    def handle(self):
        """ self.request = socket
            self.server  = ThreadedServer
            self.client_address = (address, port)
        """

        # This takes strings read from the socket and returns json objects

        self.reader = NetworkMessageReader()

        # Password test

        if self.not_authenticated():

            return

        # Enter loop
        
        while self.master.running:

            try:

                packet = self.get_message()

                # If we get none, just read in again

                if packet is None:

                    self.handle_client_lost()

                    break

            except Exception as e: # TODO be more specific

                # Handle the loss of a client

                self.handle_client_lost()

                break

            for msg in packet:

                if isinstance(msg, MSG_CONNECT):

                    # Add the new client

                    new_client = self.handle_connect(msg)

                    # Send the contents to the all clients

                    self.update_all_clients()

                    # Clear server history

                    self.master.clear_history()

                elif self.master.waiting_for_ack:

                    if isinstance(msg, MSG_CONNECT_ACK):

                        self.master.connect_ack(msg)

                else:

                    # Add any other messages to the send queue

                    self.master.msg_queue.put(msg)

        return

    def connect_clients(self, new_client):
        """ Update all other connected clients with info on new client & vice versa """

        # Store the client

        self.master.clients[new_client.id] = new_client

        # Connect each client

        msg1 = MSG_CONNECT(new_client.id, new_client.name, new_client.hostname, new_client.port)

        for client in list(self.master.clients.values()):

            # Tell other clients about the new connection

            client.send(msg1)

            # Tell the new client about other clients

            if client != self.client_address:

                msg2 = MSG_CONNECT(client.id, client.name, client.hostname, client.port)

                new_client.send(msg2)

            # Wait for handshake

            client.send(MSG_REQUEST_ACK(-1))

        return

    def update_client(self):
        """ Send all the previous operations to the client to keep it up to date """

        client = self.client()
        client.send(MSG_SET_ALL(-1, *self.master.get_contents()))

        return

    def update_all_clients(self):
        """ Sends a reset message with the contents from the server to make sure new user starts the  same  """

        msg = MSG_RESET(-1, *self.master.get_contents())

        for client in list(self.master.clients.values()):

            # Tell other clients about the new connection

            client.send(msg)

        return
    
# Keeps information about each connected client

class Client:
    bytes = TroopServer.bytes
    def __init__(self, address, id_num, request_handle, name=""):

        self.hostname = address[0]
        self.port     = address[1]
        self.address  = address
        
        self.source = request_handle

        # For identification purposes

        self.id   = int(id_num)
        self.name = name

        # Location

        self.index = 0
        
    def get_index(self):
        return self.index

    def set_index(self, i):
        self.index = i

    def __repr__(self):
        return repr(self.address)

    def send(self, message):
        try:
            self.source.sendall(message.bytes())
        except Exception as e:
            print(e)
            raise DeadClientError(self.hostname)
        return

    def __eq__(self, other):
        return self.address == other

    def __ne__(self, other):
        return self.address != other

