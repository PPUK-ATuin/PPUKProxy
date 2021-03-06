#!/usr/bin/env python

import sys
import getopt
import os
import ConfigParser
import ssl
import BaseHTTPServer
import threading
from BaseHTTPServer import BaseHTTPRequestHandler
from SocketServer import ThreadingMixIn
from ThreadPoolMixIn import ThreadPoolMixIn
from BaseHTTPServer import HTTPServer
import httplib
import traceback
from Page import Page
from JSPage import JSPage
from CSSPage import CSSPage
import Util
import Cookie
import socket
import re
import IPy
import mimetypes
import gc
import zlib
from gzip import GzipFile
from StringIO import StringIO
from multiprocessing import Pipe
from Buffer import Buffer

# Request handler for HTTP requests.
class ProxyHandler(BaseHTTPRequestHandler):
	BLKSIZE=65536

	def __init__(self,request,client_address, server):
		self.server_version = "PirateProxy/0.4"
		self.data = None
		self.good_hostname_pattern = re.compile('^([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])(\.([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]{0,61}[a-zA-Z0-9]))*|([a-f0-9:]+)$')
		self.responded_to_client = False # Have we already sent response?
		self.referer = None
		self.user_agent = None
		self.remote_host = None
		self.gzip_encoding = False
		self.gzip_out = None
		self.gzip_out_buf = None
		self.gzip_in = zlib.decompressobj(16+zlib.MAX_WBITS)
		

		try:
			BaseHTTPRequestHandler.__init__(self, request, client_address, server)
		except socket.error, e:
			pass

	def is_ssl(self):
		return False

	# Rewrite the referer URL, stripping the proxy hostname part
	def rewrite_referer(self, url):
		return Util.rewrite_URL_strip(url, self.server.config)

	# Handle the Location header in the headers, saving the newlocation
	# in the response object (bit ugly..)
	def handle_redirect(self, resp):
		if resp.status >= 300 and resp.status <= 400:
			location = resp.getheader('location', None)
			if location:
				location = Util.rewrite_URL(location, self.server.config, self.is_ssl())
			resp.newlocation = location
		else:
			resp.newlocation = None

	# Parse the incoming request's accept-encoding header to determine if we
	# can gzip
	def parse_accept_encoding(self, v):
		encodings = v.split(',')
		for encoding in encodings:
			a = encoding.split(';')
			if len(a) == 2:
				(algo, q) = a
				algo = algo.lower().strip()
				try:
					bla = q.split("=")
					if len(bla) == 2:
						q = float(bla[1].strip())
				except Exception, e:
					q = 1
			else:
				algo=a[0].lower().strip()
				q=1

			if (algo == 'gzip' or algo=='x-gzip') and q != 0:
				if self.server.config.gzip_client_response:
					self.gzip_encoding = True
					self.gzip_out_buf = Buffer()
					self.gzip_out = GzipFile(None, "wb", self.server.config.gzip_level, self.gzip_out_buf)
				return

	def do_GET(self):
		self.do_GETPOST(False)

	def do_POST(self):
		self.do_GETPOST(True)

	# Handle GET and POST requests: fetch the page, rewrite it and return 
	# it to the client. The 'post' boolean determines if this is a POST
	# request and if so, the post data is read
	def do_GETPOST(self, post):
		self.referer = self.headers.getheader('referer')
		self.user_agent = self.headers.getheader('user-agent')
		client = self.headers.getheader('x-forwarded-for')
		if client and self.server.config.use_forwarded_for:
			client = client.split(',')[-1].strip()
			self.client_address = (client, self.client_address[1])

		host = self.headers.getheader('host') or ''
		if host:
			p = host.find("."+self.server.config.hostname)
			if p != -1:
				self.remote_host = host[:p]
			else:
				self.remote_host = host
		
		self.server.reqs[threading.currentThread().name] = (self.remote_host, self.path)
		if self.handle_own() or self.handle_robot_block():
			return

		# Redirect blocked hostnames and IP addresses to block target
		if self.is_blocked(self.remote_host):
			self.send_response(302)
			self.my_log_request(302, 0)
			self.responded_to_client = True
			self.send_header("Location", self.server.config.block_target)
			self.send_header("Content-Length", 0)
			self.end_headers()
			return

		# Disallow invalid names, local networks, single label hosts and
		# loops
		if self.is_disallowed(self.remote_host):
			self.my_log_request(403, 0)
			self.send_error(403, "Access denied")
			return

		if post:
			# Read POST request itself, if not too long
			content_size = self.headers.getheader('content-length')

			if not content_size:
				size = self.server.config.max_post_size
			else:
				try:
					size = int(content_size)
				except ValueError:
					size = self.server.config_max_post_size
			
			if size > self.server.config.max_post_size:
				self.my_log_request(413, 0)
				self.send_error(413, "Request entity too large")
				return

			self.data = self.rfile.read(size)

		try:
			try:
				# Create connection object, connect to upstream server and
				# *ugly hack* set the underlying socket's timeout
				if self.server.config.upstream_proxy_address and self.server.config.upstream_proxy_port:
					if self.is_ssl():
						c = httplib.HTTPSConnection(self.server.config.upstream_proxy_address, self.server.config.upstream_proxy_port, timeout=self.server.config.upstream_connect_timeout)
					else:
						c = httplib.HTTPConnection(self.server.config.upstream_proxy_address, self.server.config.upstream_proxy_port, timeout=self.server.config.upstream_connect_timeout)
				else:
					if self.is_ssl():
						c = httplib.HTTPSConnection(self.remote_host, timeout=self.server.config.upstream_connect_timeout)
					else:
						c = httplib.HTTPConnection(self.remote_host, timeout=self.server.config.upstream_connect_timeout)
				c.connect()
				c.sock.settimeout(self.server.config.upstream_timeout)
			except Exception, e:
				# Unresolvable (or connect timeout?)
				try:
					self.send_error(504, "Gateway timeout")
					self.my_log_request(504, 0)
					c.close()
				except Exception, e:
					pass
				return

			if self.data:
				method="POST"
			else:
				method="GET"

			req_headers = {}
			for k in  self.headers:
				if k == 'host':
					req_headers[k] = self.remote_host
				elif k == 'referer':
					req_headers[k] = self.rewrite_referer(self.headers[k])
				elif k == 'connection':
					pass
				elif k == 'te':
					self.parse_accept_encoding(self.headers[k])
				elif k == 'accept-encoding' or k == 'transfer-encoding':
					self.parse_accept_encoding(self.headers[k])
					pass
				elif k in self.server.config.filter_headers:
					pass
				else:
					req_headers[k] = self.headers[k]	
		
			if self.server.config.gzip_server_response:
				req_headers['Accept-Encoding']='gzip'
			req_headers['Connection'] = 'close'
			if self.server.config.upstream_proxy_address and self.server.config.upstream_proxy_port:
				if self.is_ssl():
					req = c.request(method, "https://"+self.remote_host+self.path, self.data, req_headers)
				else:
					req = c.request(method, "http://"+self.remote_host+self.path, self.data, req_headers)
			else:
				req = c.request(method, self.path, self.data, req_headers)
			resp = c.getresponse()

			# Handle redirects correctly
			self.handle_redirect(resp)

			content_type = resp.msg.gettype()

			if content_type in ["text/html"]:
				self.handle_rewritable(resp, Page)
			elif content_type in ["application/xhtml+xml", "application/xml", "application/xhtml" ]:
				self.handle_rewritable(resp, Page)
			elif content_type in ["text/javascript", "application/json", "application/x-javascript"]:
				self.handle_rewritable(resp, JSPage)
			elif content_type in ["text/css"]:
				self.handle_rewritable(resp, CSSPage)
			else:
				self.handle_content(resp)

		except Exception, e:
			# Any exception results in an internal server error
			if type(e) != socket.error:
				self.my_log_error(traceback.format_exc())

			# Do not send error if we already have responded to the client
			if not self.responded_to_client:
				try:
					if type(e) == socket.timeout:
						self.send_error(504)
						self.my_log_request(504, 0)
					else:
						self.send_error(500)
						self.my_log_request(504, 0)
					c.close()
				except Exception, e:
					pass
			return

		try:
			c.close()
		except Exception, e:
			pass

	# Rewrite the cookie to have the correct domain attribute
	def rewrite_cookie(self, cookie):
		try:
			c = Cookie.SimpleCookie(cookie)

			for cookiename in c:
				domain = c[cookiename].get('domain')
				if domain:
					# Need to strip as sometimes at least ',' is retained
					domain = domain.strip(' \t\r\n,;')
					domain = domain + "." + self.server.config.hostname
					c[cookiename]['domain'] = domain
			cookie = c.output()
		except Exception, e:
			self.my_log_error(traceback.format_exc())
			pass

		return cookie

	# Handle HTML, JS and CSS pages
	def handle_rewritable(self, resp, rewriter_class):
		self.resp = resp
		try:
			self.content_length = int(resp.getheader('content-length', -1))
		except ValueError,e:
			self.content_length = -1

		if resp.getheader('content-encoding') == 'gzip' or resp.getheader('transfer-encoding') == 'gzip':
			self.gzip_from_server = True
		else:
			self.gzip_from_server = False


		self.my_log_request(resp.status, self.content_length)

		# Write the response headers
		headerstr='HTTP/1.0 %d %s\r\n' % (resp.status, resp.reason)
		headerstr+='Server: %s\r\n' % (self.server_version)
		headerstr+='Date: %s\r\n' % (self.date_time_string())

		for (k, v) in resp.getheaders():
			if k in ["server", "date", "content-length", "transfer-encoding", "content-encoding", "connection"]:
				continue
			elif k == "set-cookie":
				headerstr += self.rewrite_cookie(v)+"\r\n"
				continue
			elif k == "location" and resp.newlocation:
				v = resp.newlocation

			if k:
				headerstr += "%s: %s\r\n" % (k, v)

		headerstr += "Connection: close\r\n"
		if self.gzip_encoding:
			headerstr += "Content-Encoding: gzip\r\n"

		self.wfile.write(headerstr+'\r\n')
		self.responded_to_client = True

		# Rewrite content and send to client. Reader and writer functions
		# are given to the Page, JSPage or CSSPage instance to read blocks
		# of data from the server response and write blocks of data to the
		# client. Gzip-handling is done in the reader/writer.
		p = rewriter_class(self.server.config, self.is_ssl(), self.reader, self.writer)
		p.rewrite()
	

	# Function used to read blocks from the server response.
	# GZIP decompresses if necessary
	def reader(self, BLKSIZE):
		if self.gzip_from_server:
			s = self.resp.read(BLKSIZE)
			return self.gzip_in.decompress(s)
		else:
			return(self.resp.read(BLKSIZE))
			
	# Function used to write blocks to the client
	# GZIP compresses if necessary
	def writer(self, string):
		if self.gzip_encoding:
			self.gzip_out.write(string)
			self.gzip_out.flush()
			self.wfile.write(self.gzip_out_buf.read())
		else:
			self.wfile.write(string)


	def handle_content(self, resp):
		headerstr='HTTP/1.0 %d %s\r\n' % (resp.status, resp.reason)
		headerstr+='Server: %s\r\n' % (self.server_version)
		headerstr+='Date: %s\r\n' % (self.date_time_string())
		for (k, v) in resp.getheaders():
			if k in ["content-length", "server", "date", "content-encoding", "transfer-encoding"]:
				continue
			elif k == "set-cookie":
				v = self.rewrite_cookie(v)
			elif k == "location" and resp.newlocation:
				v = resp.newlocation

			headerstr += "%s: %s\r\n" % (k, v)

		try:
			content_length = int(resp.getheader('content-length', -1))
		except ValueError:
			content_length = -1
		cl = content_length
		if cl == -1:
			cl = 0
		self.my_log_request(resp.status, cl)

		if self.gzip_encoding:
			headerstr+="Content-Encoding: gzip\r\n"

		if resp.getheader('content-encoding') == 'gzip' or resp.getheader('transfer-encoding') == 'gzip':
			self.gzip_from_server = True
		else:
			self.gzip_from_server = False


		self.resp = resp

		# Two cases:
		# 1) Response is gzip encoded and client accepts it. Send one-to-one.
		# 2) Response is not encoded but client accepts it or vice-versa.

		if self.gzip_encoding and self.gzip_from_server:
			# (1)
			reader = self.resp.read
			writer = self.wfile.write
			if content_length >= 0:
				headerstr += "Content-Length: "+str(content_length)+"\r\n"
		else:
			# (2)
			# Use the standard reader/writer which handle gzip decompression
			# and compression automagically
			reader = self.reader
			writer = self.writer

			# As we do not know the resulting size in advance, do not send
			# a content-length field, but close the connection afterwards
			# in stead.
			headerstr += "Connection: close\r\n"

		self.wfile.write(headerstr+"\r\n")
		self.responded_to_client = True

		while True:
			s = reader(self.BLKSIZE)
			if not s or len(s) == 0:
				break
			writer(s)

	# Returns true if the remote host is blocked
	def is_blocked(self, remote_host):
		ips = []
		try:
			ips = [ remote_host ]
			ips.extend(socket.gethostbyname_ex(remote_host)[2])
		except Exception, e:
			pass

		for ip in ips:
			if ip in self.server.config.blocked_sites:
				return True

		return False


	# Returns true if the remote_host is not allowed: its name is too long,
	# it is not a correct hostname, contains only a single label or
	# its addresses or the hostname itself are part of private networks
	def is_disallowed(self, remote_host):
		if len(remote_host) > 255 - len(self.server.config.hostname):
			return True

		m = re.match(self.good_hostname_pattern, remote_host)
		if not m:
			return True

		if remote_host.endswith(self.server.config.hostname):
			return True

		ips = []
		try:
			ips = [ remote_host ]
			ips.extend(socket.gethostbyname_ex(remote_host)[2])
		except Exception, e:
			pass

		for ip in ips:
			try:
				ipaddr = IPy.IP(ip)
			except Exception, e:
				# Not an IP address, check if its a single label host
				if remote_host.find('.') == -1: # Single label host
					return True
				pass
			else:
				for net in ['227.0.0.0/8', '10.0.0.0/8', '192.168.0.0/16', '172.16.0.0/12', '224.0.0.0/3', '169.254.0.0/16', '0.0.0.0/8', '::1/128', 'FC00::/7', 'FE00::/9', 'FE80::/10','FEC0::/10', 'FF00::/8' ]:
					if ipaddr.overlaps(net):
						return True

	# If robots should be blocked and '/robots.txt' is requested, then
	# send a fake robots.txt blocking robots. Returns True if we send
	# the fake robots.txt
	def handle_robot_block(self):
		fakerobots = '# Inserted by '+self.server.config.hostname+'\r\n'
		fakerobots += 'User-Agent: *\r\nDisallow: /\r\n'
		size = len(fakerobots)

		if not (self.server.config.block_robots and self.path.lower()=='/robots.txt'):
			return False
		
		self.my_log_request(200, size)
		self.send_response(200)
		self.responded_to_client = True
		self.send_header("Content-Length", str(size))
		self.send_header("Content-Type", "text/plain")
		self.end_headers()
		self.wfile.write(fakerobots)
		return True


	# Handle requests destined for the proxy itself, such as the initial
	# page and authentication requests. Also handles requests for unknown 
	# hostnames or non-existing host-header. Returns true if request was 
	# handled here.
	def handle_own(self):
		host = self.headers.getheader('host')

		if host:
			a = host.rfind(":")
			if a != -1 and len(host) > a:
				if host[a+1:].isdigit():
					host = host[:a]
		else:
			host = ''

		if host.endswith(self.server.config.hostname) and host != self.server.config.hostname:
			return False

		# This is for us, so handle it
		try:
			if os.path.isdir(self.server.config.files_location+"/"+self.path):
				self.path += '/index.html'
		except Exception, e:
			pass

		if self.path.find('..') != -1:
			self.my_log_request(403, 0)
			self.send_error(403)
			return True

		self.path = os.path.normpath(self.path)
		self.handle_file(self.server.config.files_location+"/"+self.path)
		return True

	def handle_file(self, fn):
		try:
			f = open(fn)
			s = os.stat(fn).st_size
		except Exception, e:
			self.my_log_request(404, 0)
			self.send_error(404)
			try:
				f.close()
			except UnboundLocalError, e:
				pass
			return

		content_type = mimetypes.guess_type(fn, False)[0] or 'application/octet-stream'
		self.my_log_request(200,s)
		self.send_response(200)
		self.responded_to_client = True
		self.send_header("Content-Size", str(s))
		self.send_header("Content-Type", content_type)
		self.end_headers()
		self.wfile.write(f.read())
		f.close()


	# Log a request to error log
	def my_log_error(self, error_msg):
		if self.referer:
			referer = '"'+self.referer+'"'
		else:
			referer = '-'
		if self.user_agent:
			user_agent = '"' + self.user_agent+'"'
		else:
			user_agent = '-'
		if self.is_ssl():
			scheme = 'https'
		else:
			scheme = 'http'
		if self.remote_host and len(self.remote_host) > 0:
			remote_host = self.remote_host
		else:
			remote_host = self.server.config.hostname

		size = "-"

		error_msg='"'+ error_msg + '"'
		status = 500
		msg = "%s - - [%s] %s %s://%s%s %s %d %s %s %s %s"  % (self.address_string(), self.log_date_time_string(), self.command, scheme, remote_host, self.path, self.request_version, status, size, referer, user_agent, error_msg)
		self.server.log_error(msg)

	# Log a request to access log
	def my_log_request(self, status, size):
		if self.referer:
			referer = '"'+self.referer+'"'
		else:
			referer = '-'
		if self.user_agent:
			user_agent = '"' + self.user_agent+'"'
		else:
			user_agent = '-'
		if self.is_ssl():
			scheme = 'https'
		else:
			scheme = 'http'
		if self.remote_host and len(self.remote_host) > 0:
			remote_host = self.remote_host
		else:
			remote_host = self.server.config.hostname

		if size == -1:
			size = "-"
		else:
			size = str(size)

		msg = "%s - - [%s] %s %s://%s%s %s %d %s %s %s"  % (self.address_string(), self.log_date_time_string(), self.command, scheme, remote_host, self.path, self.request_version, status, size, referer, user_agent)
		self.server.log_access(msg)

	# Override the log_request() method to not log anything. 
	def log_request(self, code="-", size="-"):
		pass

	# Override the log_error() method to not log anything. 
	def log_error(self, format, *args):
		pass

	# Override the address_string method to resolve the client address based on
	# the configuration setting
	def address_string(self):
		if self.server.config.client_resolve:
			return socket.getfqdn(self.client_address[0])
		else:
			return self.client_address[0]
	
# Request handler for HTTPS requests. 
class ProxySSLHandler(ProxyHandler):
	def __init__(self, request, client_address, server):
		ProxyHandler.__init__(self, request, client_address, server)

	def is_ssl(self):
		return True

# HTTPServer class that implements threading and a read/write timeout
class ThreadedHTTPServer(ThreadPoolMixIn, HTTPServer):
	def process_request(self, request, client_address):
		request.settimeout(self.config.client_timeout)
		ThreadPoolMixIn.process_request(self, request, client_address)

	def open_logs(self):
		if self.config.access_log:
			try:
				self.access_log = file(self.config.access_log, "a")
			except Exception, e:
				print >> sys.stderr, "Error opening access log %s: %s" % (self.config.access_log, traceback.format_exc())
				self.access_log = sys.stdout
		else:
			self.access_log = sys.stdout

		if self.config.error_log:
			try:
				self.error_log = file(self.config.error_log, "a")
			except Exception, e:
				print >> sys.stderr, "Error opening error log %s: %s" % (self.config.access_log, traceback.format_exc())
				self.error_log = sys.stderr
		else:
			self.error_log = sys.stderr

	def log_access(self, msg):
		self.access_log.write(msg+"\n")

	def log_error(self, msg):
		self.error_log.write(msg+"\n")

# Main application class, implements a HTTP and HTTPS URL rewriting proxy
# server
class Proxy:
	def __init__(self, config):
		self.config = config
		self.http_server = ThreadedHTTPServer(('', self.config.http_listen_port), ProxyHandler)
		self.http_server.config = config
		self.http_server.timeout = config.client_timeout 
		self.http_server.open_logs()

		if self.config.https_certificate:
			self.https_server = ThreadedHTTPServer(('', self.config.https_listen_port), ProxySSLHandler)
			self.https_server.socket = ssl.wrap_socket(self.https_server.socket, certfile=self.config.https_certificate, server_side=True)
			self.https_server.config = config
			self.https_server.timeout = config.client_timeout
			self.https_server.open_logs()
		else:
			self.https_server = None

	# Start a HTTP and a HTTPS thread to handle connections		
	def start(self):
		self.http_thread = threading.Thread(target=self.run, kwargs={'server': self.http_server})
		self.http_thread.daemon = True
		self.http_thread.start()

		if self.https_server:
			self.https_thread = threading.Thread(target=self.run, kwargs={'server': self.https_server})
			self.https_thread.daemon = True
			self.https_thread.start()

		while True:
			try:
				self.http_thread.join(60)
				if self.https_server:
					self.https_thread.join(60)
			except KeyboardInterrupt:
				sys.exit(2)
	
	# Handle connections indefinitely
	def run(self, server):
		#server.serve_forever()
		server.serve_forever(numThreads = self.config.threadpool_size)

class Config:
	def __init__(self):
		self.hostname = None
		self.http_port = 0
		self.https_port = 0
		self.rewrites=[]
		self.http_listen_port = 0
		self.https_listen_port = 0
		self.https_certificate = None
		self.upstream_proxy_address = None
		self.upstream_proxy_port = 0
		self.max_page_size = 5242880
		self.max_post_size = 1048576
		self.upstream_timeout=30
		self.upstream_connect_timeout=10
		self.client_timeout = 30
		self.client_resolve = False
		self.files_location = 'html'
		self.filter_headers = ['x-forwarded-for']
		self.block_robots = True
		self.rewrites = []
		self.gzip_level = 9
		self.gzip_client_response = True
		self.gzip_server_response = True
		self.use_forwarded_for = False
		self.threadpool_size = 64
		self.block_list = None
		self.block_target = None
		self.blocked_sites = self.parse_block_list(self.block_list)
		self.access_log = None
		self.error_log = None

		(opts, rest) = getopt.getopt(sys.argv[1:], "c:")

		for (o, a) in opts:
			if o == "-c":
				self.parseConfig(a)

		if not self.validConfig():
			self.usage()
			sys.exit(1)

	# Shows usage information
	def usage(self):
		print "Usage: %s [-c configfile ]" % (sys.argv[0])

	# Returns True if the current configuration is sound
	def validConfig(self):
		if not self.hostname:
			return False
		if self.http_listen_port < 1 or self.http_listen_port > 65535 or \
		   self.https_listen_port < 1 or self.https_listen_port > 65535:
			return False
		if self.http_port < 1 or self.http_port > 65535 or \
		   self.https_port < 1 or self.https_port > 65535:
			return False
		return True

	# Parses the supplied config file and fills in class variables
	def parseConfig(self, fn):
		try:
			conf = ConfigParser.ConfigParser()
			conf.read(fn)
			self.conf = conf
			self.hostname = conf.get('global', 'hostname').lower()
			self.http_listen_port = conf.getint('global', 'http_listen_port')
			self.https_listen_port = conf.getint('global', 'https_listen_port')
			self.http_port = conf.getint('global', 'http_port')
			self.https_port = conf.getint('global', 'https_port')
			self.https_certificate = conf.get('global','https_certificate')
			self.upstream_proxy_address = conf.get('global','upstream_proxy_address')
			self.upstream_proxy_port = conf.getint('global','upstream_proxy_port')
			self.max_page_size = conf.getint('global', 'max_page_size')
			self.max_post_size = conf.getint('global', 'max_post_size')
			self.upstream_timeout=conf.getint('global', 'upstream_timeout')
			self.upstream_connect_timeout=conf.getint('global', 'upstream_connect_timeout')
			self.client_timeout = conf.getint('global', 'client_timeout')
			self.client_resolve = conf.getboolean('global', 'client_resolve')
			self.files_location = conf.get('global', 'files_location')
			self.filter_headers = conf.get('global', 'filter_headers').split(',')			
			self.block_robots= conf.getboolean('global', 'block_robots')
			self.rewrites = conf.items('rewrites')
			self.gzip_level = conf.getint('global', 'gzip_level')
			self.gzip_client_response = conf.getboolean('global', 'gzip_client_response')
			self.gzip_server_response = conf.getboolean('global', 'gzip_server_response')
			self.use_forwarded_for = conf.getboolean('global', 'use_forwarded_for')
			self.threadpool_size = conf.getint('global', 'threadpool_size')
			self.block_list = conf.get('global', 'block_list')
			self.block_target = conf.get('global', 'block_target')
			self.blocked_sites = self.parse_block_list(self.block_list)
			self.access_log = conf.get('global', 'access_log')
			self.error_log = conf.get('global', 'error_log')

		except Exception, e:
			print "Error while parsing configuration file:", e
			# We'll handle incorrect configuration later
			pass

	# Reads the block list into a hashtable 
	def parse_block_list(self, fn):
		retval = {}
		try:
			f = open(fn)
			l = f.readlines()
		except Exception, e:
			l = []
		b = [ blocked.strip().lower() for blocked in l ]
		for blocked in b:
			retval[blocked] = True
		return retval

if __name__=="__main__":
	c = Config()
	p = Proxy(c)

	p.start()

